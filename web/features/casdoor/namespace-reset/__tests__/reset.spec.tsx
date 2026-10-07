import type { CasdoorConfigurationResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { zCasdoorConfigurationResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { consoleQuery } from '@/service/console'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { NamespaceReset } from '..'

const { transport } = vi.hoisted(() => ({ transport: vi.fn() }))
vi.mock('@/service/base', () => ({ request: transport }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example.test/console/api' }))
vi.mock('@/env', () => ({
  env: { NEXT_PUBLIC_API_PREFIX: 'https://console.example.test/console/api' },
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  return createReactI18nextMock((await import('@/i18n/en-US/extend.json')).default)
})
const account = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const namespace = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const revision = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const nextNamespace = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const nextRevision = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
const config: CasdoorConfigurationResponse = {
  enabled: false,
  etag: 3,
  active: null,
  active_revision_id: null,
  draft_revision_id: revision,
  draft: {
    revision_id: revision,
    namespace_id: namespace,
    secret_configured: true,
    configuration: {
      application: 'synthetic',
      organization: 'synthetic',
      client_id: 'synthetic',
      backend_api_url: 'https://idp.example.test',
      browser_frontend_url: 'https://idp.example.test',
      expected_issuer: 'https://idp.example.test',
      default_workspace_id: revision,
    },
  },
}
const next: CasdoorConfigurationResponse = {
  ...config,
  etag: 4,
  draft_revision_id: nextRevision,
  draft: { ...config.draft!, revision_id: nextRevision, namespace_id: nextNamespace },
}
const review = {
  review_id: 'R'.repeat(43),
  namespace_id: namespace,
  etag: 3,
  expires_in: 60,
  credential_check: 'format_only',
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })
let client: QueryClient
let refresh: ReturnType<typeof vi.fn<() => Promise<CasdoorConfigurationResponse | null>>>
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient()
  seedAccountProfileQuery(client, { id: account })
  client.setQueryData(
    consoleQuery.systemManageExtend.integration.casdoor.get.queryKey(),
    zCasdoorConfigurationResponse.parse(config),
  )
  refresh = vi.fn().mockResolvedValue(next)
  transport.mockImplementation(async (_url, _init, options) =>
    json(new URL(options.request.url).pathname.endsWith('/review') ? review : next),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  vi.useRealTimers()
})
function mount(response = config, refreshing = false, refreshError: unknown = null) {
  return render(
    <QueryClientProvider client={client}>
      <NamespaceReset
        response={response}
        refreshing={refreshing}
        refreshError={refreshError}
        onRefresh={refresh}
      />
    </QueryClientProvider>,
  )
}
async function prepare(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
  )
  await user.click(screen.getByRole('button', { name: 'Review namespace reset' }))
  await screen.findByText(
    'Password fields passed a format check only. Password sign-in and new adoption have not been verified.',
  )
}
describe('namespace reset through the official generated manager API', () => {
  it('requires both explicit confirmations and an actual configuration refresh', async () => {
    mount()
    const user = userEvent.setup()
    expect(screen.getByRole('button', { name: 'Review namespace reset' })).toBeDisabled()
    await prepare(user)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    await screen.findByText(
      'Current configuration is a new disabled draft. Existing rows are preserved.',
    )
    expect(refresh).toHaveBeenCalledOnce()
    expect(await transport.mock.calls[0]![2].request.clone().json()).toEqual({
      etag: 3,
      namespace_id: namespace,
      confirm_management_review: true,
    })
    expect(await transport.mock.calls[1]![2].request.clone().json()).toEqual({
      etag: 3,
      review_id: review.review_id,
    })
  })
  it.each(['enabled', 'fetching', 'error', 'missing-namespace'] as const)(
    'does not expose reset authority for %s',
    (state) => {
      mount(
        state === 'enabled'
          ? { ...config, enabled: true }
          : state === 'missing-namespace'
            ? { ...config, draft: null, draft_revision_id: null }
            : config,
        state === 'fetching',
        state === 'error' ? new Error('synthetic configuration read failure') : null,
      )
      expect(
        screen.queryByRole('button', { name: 'Review namespace reset' }),
      ).not.toBeInTheDocument()
      expect(transport).not.toHaveBeenCalled()
    },
  )
  it('keeps an unknown submission unknown and never reuses the consumed review', async () => {
    mount()
    const user = userEvent.setup()
    await prepare(user)
    transport.mockResolvedValueOnce(json({}, 500))
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    await screen.findByText(
      'Reset submission is unconfirmed. Refresh current configuration; do not resubmit this review.',
    )
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    expect(refresh).toHaveBeenCalledOnce()
    expect(transport).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(
      screen.queryByText(
        'Current configuration is a new disabled draft. Existing rows are preserved.',
      ),
    ).not.toBeInTheDocument()
  })
  it.each(['review', 'reset'] as const)(
    'sends only one %s request for same-tick native submissions',
    async (stage) => {
      mount()
      const user = userEvent.setup()
      if (stage === 'reset') {
        await prepare(user)
        await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
      } else
        await user.click(
          screen.getByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
        )
      transport.mockImplementation(() => new Promise(() => {}))
      const button = screen.getByRole('button', {
        name: stage === 'review' ? 'Review namespace reset' : 'Confirm',
      })
      const before = transport.mock.calls.length
      act(() => {
        button
          .closest('form')!
          .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
        button
          .closest('form')!
          .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      })
      await waitFor(() => expect(transport.mock.calls.length).toBeGreaterThan(before))
      expect(transport.mock.calls.length - before).toBe(1)
    },
  )
  it.each(['fetch', 'error'] as const)(
    'destroys a reviewed handle after configuration %s even if cached data recovers',
    async (kind) => {
      mount()
      const user = userEvent.setup()
      await prepare(user)
      await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
      const query = client.getQueryCache().find({
        queryKey: consoleQuery.systemManageExtend.integration.casdoor.get.queryKey(),
        exact: true,
      })!
      act(() => {
        query.setState(
          kind === 'fetch'
            ? { fetchStatus: 'fetching' }
            : { status: 'error', error: new Error('synthetic') },
        )
        query.setState({ status: 'success', error: null, fetchStatus: 'idle' })
      })
      expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    },
  )
  it('clears both consent and confirmation after an idle configuration data replacement', async () => {
    mount()
    const user = userEvent.setup()
    await prepare(user)
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
    act(() =>
      client.setQueryData(
        consoleQuery.systemManageExtend.integration.casdoor.get.queryKey(),
        zCasdoorConfigurationResponse.parse(next),
      ),
    )
    expect(
      screen.getByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
    ).not.toBeChecked()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review namespace reset' })).toBeDisabled()
  })
  it('checks elapsed TTL at native submit even before the browser timer runs', async () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    mount()
    const user = userEvent.setup()
    await prepare(user)
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
    now = 60001
    const form = screen.getByRole('button', { name: 'Confirm' }).closest('form')!
    act(() => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(transport).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
  })
  it.each(['active', 'etag-jump', 'same-namespace'] as const)(
    'rejects a misleading successful reset response: %s',
    async (kind) => {
      mount()
      const user = userEvent.setup()
      await prepare(user)
      transport.mockResolvedValueOnce(
        json(
          kind === 'active'
            ? { ...next, active: next.draft, active_revision_id: nextRevision }
            : kind === 'etag-jump'
              ? { ...next, etag: 5 }
              : config,
        ),
      )
      await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
      await user.click(screen.getByRole('button', { name: 'Confirm' }))
      await screen.findByText(
        'Reset submission is unconfirmed. Refresh current configuration; do not resubmit this review.',
      )
      expect(refresh).not.toHaveBeenCalled()
      expect(
        client.getQueryState(consoleQuery.systemManageExtend.integration.casdoor.get.queryKey())
          ?.isInvalidated,
      ).toBe(true)
    },
  )
  it('does not restore a late review after configuration fetch and same-data recovery', async () => {
    let resolve!: (response: Response) => void
    transport.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done
        }),
    )
    mount()
    const user = userEvent.setup()
    await user.click(
      screen.getByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
    )
    await user.click(screen.getByRole('button', { name: 'Review namespace reset' }))
    const query = client.getQueryCache().find({
      queryKey: consoleQuery.systemManageExtend.integration.casdoor.get.queryKey(),
      exact: true,
    })!
    act(() => {
      query.setState({ fetchStatus: 'fetching' })
      query.setState({ fetchStatus: 'idle' })
    })
    await act(async () => {
      resolve(json(review))
      await Promise.resolve()
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Review namespace reset' })).toBeDisabled(),
    )
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
  })
  it('rejects a late review across A → B → A on the real profile cache', async () => {
    let resolve!: (response: Response) => void
    transport.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done
        }),
    )
    mount()
    const user = userEvent.setup()
    await user.click(
      screen.getByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
    )
    await user.click(screen.getByRole('button', { name: 'Review namespace reset' }))
    act(() => {
      seedAccountProfileQuery(client, { id: nextNamespace })
      seedAccountProfileQuery(client, { id: account })
    })
    await act(async () => {
      resolve(json(review))
      await Promise.resolve()
    })
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument(),
    )
  })
})
