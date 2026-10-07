import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { AvatarRetry } from '..'

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
const otherAccount = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const identity = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const intent = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const target = {
  account_id: account,
  identity_id: identity,
  intent_id: intent,
  reason: 'image_rejected',
  retry_eligible: true,
}
const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
const targets = { targets: [target], next_after: null, has_more: false }
let client: QueryClient
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient()
  seedAccountProfileQuery(client, { id: account })
  transport.mockImplementation(async (_url, _init, options) =>
    options.request.method === 'POST'
      ? json({ status: 'pending', intent_id: intent })
      : json(targets),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
})

function mount() {
  render(
    <QueryClientProvider client={client}>
      <AvatarRetry />
    </QueryClientProvider>,
  )
}

describe('avatar retry source changes in the live profile query cache', () => {
  it.each(['remove', 'error'] as const)(
    'removes retry targets from the UI when the profile query is %s',
    async (change) => {
      mount()
      await screen.findByRole('combobox', { name: 'Account and avatar operation' })
      const profile = client
        .getQueryCache()
        .find({ queryKey: userProfileQueryOptions().queryKey, exact: true })!

      act(() => {
        if (change === 'remove') client.removeQueries({ queryKey: profile.queryKey, exact: true })
        else profile.setState({ status: 'error', error: new Error('profile refresh failed') })
      })

      expect(
        screen.queryByRole('combobox', { name: 'Account and avatar operation' }),
      ).not.toBeInTheDocument()
      expect(transport.mock.calls.filter((call) => call[2].request.method === 'POST')).toHaveLength(
        0,
      )
    },
  )

  it('does not let a late target response cross a same-clock A → B → A profile update', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1000)
    const pendingReads: Array<(response: Response) => void> = []
    transport.mockImplementation((_url, _init, options) =>
      options.request.method === 'POST'
        ? Promise.resolve(json({ status: 'pending', intent_id: intent }))
        : new Promise<Response>((resolve) => pendingReads.push(resolve)),
    )
    mount()
    await waitFor(() => expect(pendingReads).toHaveLength(1))

    act(() => {
      seedAccountProfileQuery(client, { id: otherAccount })
      seedAccountProfileQuery(client, { id: account })
    })
    await waitFor(() => expect(pendingReads).toHaveLength(2))
    expect(client.getQueryState(userProfileQueryOptions().queryKey)?.dataUpdatedAt).toBe(1000)

    await act(async () => {
      pendingReads[0]!(json(targets))
      await Promise.resolve()
    })
    expect(
      screen.queryByRole('combobox', { name: 'Account and avatar operation' }),
    ).not.toBeInTheDocument()

    await act(async () => {
      pendingReads[1]!(json(targets))
      await Promise.resolve()
    })
    expect(
      await screen.findByRole('combobox', { name: 'Account and avatar operation' }),
    ).toBeInTheDocument()
    clock.mockRestore()
  })
})
