import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { consoleQuery } from '@/service/console'
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
const identity = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const intent = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const target = {
  account_id: account,
  identity_id: identity,
  intent_id: intent,
  reason: 'image_rejected',
  retry_eligible: true,
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })
let client: QueryClient
let targets: unknown
let post: () => Promise<Response>
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient()
  seedAccountProfileQuery(client, { id: account })
  targets = { targets: [target], next_after: null, has_more: false }
  post = async () => json({ status: 'pending', intent_id: intent })
  transport.mockImplementation(async (_url, _init, options) =>
    options.request.method === 'POST' ? post() : json(targets),
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
async function choose(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(
    await screen.findByRole('combobox', { name: 'Account and avatar operation' }),
    intent,
  )
  await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
}
const writes = () => transport.mock.calls.filter((call) => call[2].request.method === 'POST')
describe('explicit avatar retry through the official generated manager API', () => {
  it('submits one intent and reports only the returned pending state', async () => {
    mount()
    const user = userEvent.setup()
    await choose(user)
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    await screen.findByText(
      'Retry request is pending. Avatar execution and attachment are not confirmed.',
    )
    expect(writes()).toHaveLength(1)
    expect(await writes()[0]![2].request.clone().json()).toEqual({ intent_id: intent })
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
  })
  it.each(['absent', 'still-eligible'] as const)(
    'keeps an unknown POST unknown when current targets are %s',
    async (result) => {
      mount()
      const user = userEvent.setup()
      await choose(user)
      post = async () => {
        targets = {
          targets: result === 'absent' ? [] : [target],
          next_after: null,
          has_more: false,
        }
        return json({}, 500)
      }
      await user.click(screen.getByRole('button', { name: 'Confirm' }))
      await screen.findByText(
        'Retry submission is unconfirmed. Current targets do not prove its outcome.',
      )
      await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
      await waitFor(() => expect(writes()).toHaveLength(1))
      expect(
        screen.queryByText(
          'Retry request is pending. Avatar execution and attachment are not confirmed.',
        ),
      ).not.toBeInTheDocument()
      if (result === 'still-eligible') {
        await user.selectOptions(
          screen.getByRole('combobox', { name: 'Account and avatar operation' }),
          intent,
        )
        expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
      }
    },
  )
  it.each(['completed', 'wrong-intent'] as const)(
    'keeps a malformed successful POST unknown: %s',
    async (kind) => {
      mount()
      const user = userEvent.setup()
      await choose(user)
      const selfKey = consoleQuery.account.casdoorIdentity.get.key()
      client.setQueryData<unknown>(selfKey, { synthetic: true })
      post = async () =>
        json({
          status: kind === 'completed' ? 'completed' : 'pending',
          intent_id: kind === 'wrong-intent' ? identity : intent,
        })
      await user.click(screen.getByRole('button', { name: 'Confirm' }))
      await screen.findByText(
        'Retry submission is unconfirmed. Current targets do not prove its outcome.',
      )
      expect(writes()).toHaveLength(1)
      expect(client.getQueryState(selfKey)?.isInvalidated).toBe(true)
      expect(
        screen.queryByText(
          'Retry request is pending. Avatar execution and attachment are not confirmed.',
        ),
      ).not.toBeInTheDocument()
    },
  )
  it('sends one POST for same-tick native form submissions', async () => {
    mount()
    const user = userEvent.setup()
    await choose(user)
    post = () => new Promise(() => {})
    const form = screen.getByRole('button', { name: 'Confirm' }).closest('form')!
    act(() => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    await waitFor(() => expect(writes().length).toBeGreaterThan(0))
    expect(writes()).toHaveLength(1)
  })
  it('can advance an empty filtered page using the actual server cursor', async () => {
    targets = { targets: [], next_after: intent, has_more: true }
    mount()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Next page' }))
    await waitFor(() =>
      expect(
        transport.mock.calls.some(
          (call) => new URL(call[2].request.url).searchParams.get('after') === intent,
        ),
      ).toBe(true),
    )
  })
  it('rejects a backwards server cursor after advancing a legitimate empty filtered page', async () => {
    targets = { targets: [], next_after: intent, has_more: true }
    mount()
    const user = userEvent.setup()
    const next = await screen.findByRole('button', { name: 'Next page' })
    targets = { targets: [], next_after: identity, has_more: true }
    await user.click(next)
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Next page' })).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
  it('hides targets when logout clears the actual query cache', async () => {
    mount()
    await screen.findByRole('combobox', { name: 'Account and avatar operation' })
    act(() => client.clear())
    expect(
      screen.queryByRole('combobox', { name: 'Account and avatar operation' }),
    ).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
})
