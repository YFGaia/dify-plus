import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { CSRF_COOKIE_NAME } from '@/config'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { LocalLifecycle } from '..'
import { localInspectionOptions } from '../client'

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
const prefix = '/console/api/system-manage-extend/integration/casdoor/local-membership'
let client: QueryClient
let requests: Request[]
beforeEach(() => {
  vi.stubGlobal('Request', NativeRequest)
  client = new QueryClient()
  seedAccountProfileQuery(client, { id: account })
  requests = []
  document.cookie = `${CSRF_COOKIE_NAME()}=synthetic-local-csrf; path=/`
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request.clone())
      const path = new URL(request.url).pathname
      if (path.endsWith('/targets'))
        return Response.json({
          items: [
            {
              identity_id: identity,
              workspace_id: workspace,
              account_id: account,
              account_name: 'Synthetic account',
              workspace_name: 'Synthetic workspace',
              current_role: 'normal',
            },
          ],
          has_more: false,
          next_identity_id: null,
          next_workspace_id: null,
        })
      if (path === prefix)
        return Response.json({
          identity_id: identity,
          workspace_id: workspace,
          account_id: account,
          etag: 3,
          current_role: 'normal',
          ownership: 'managed',
          local_no_intent: true,
        })
      if (path.endsWith('/review'))
        return Response.json({
          review_id: 'R'.repeat(43),
          etag: 3,
          operation: 'release',
          current_role: 'normal',
          target_role: 'normal',
          expires_in: 60,
        })
      return Response.json({
        status: 'released',
        membership_id: 'd'.repeat(36),
        ownership_epoch: 4,
      })
    }),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
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

describe('independent LOCAL lifecycle source-freshness checks', () => {
  it('disables confirmation when the inspected owner ETag changes after review', async () => {
    const user = userEvent.setup()
    mount()
    await choose(user)
    await user.click(screen.getByRole('button', { name: 'Review release' }))
    const confirm = await screen.findByRole('checkbox', { name: 'I confirm this reviewed change.' })
    await user.click(confirm)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled()

    const inspectionOptions = localInspectionOptions(client, identity, workspace)
    const inspection = client
      .getQueryCache()
      .find({ queryKey: inspectionOptions.queryKey, exact: true })
    expect(inspection?.state.data).toMatchObject({ etag: 3, ownership: 'managed' })
    await act(async () => {
      client.setQueryData(inspectionOptions.queryKey, {
        ...(inspection!.state.data as Record<string, unknown>),
        etag: 4,
      })
    })
    expect(client.getQueryData(inspectionOptions.queryKey)).toMatchObject({ etag: 4 })

    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled())
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(writes()).toHaveLength(1)
    expect(await writes()[0]!.json()).toMatchObject({ operation: 'release', etag: 3 })
  })
})
