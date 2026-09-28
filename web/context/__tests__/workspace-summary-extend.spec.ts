import type { GetWorkspacesCurrentSummaryResponse } from '@dify/contracts/api/console/workspaces/types.gen'
import { QueryClient } from '@tanstack/react-query'
import { createStore } from 'jotai'
import { queryClientAtom } from 'jotai-tanstack-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { consoleQuery } from '@/service/console'
import { adminExtendAtom, tenantExtendAtom } from '../app-context-extend'
import { normalizeCurrentWorkspaceSummary } from '../app-context-normalizers'
import {
  currentWorkspaceAtom,
  currentWorkspaceLoadingAtom,
  isCurrentWorkspaceManagerAtom,
  isCurrentWorkspaceOwnerAtom,
} from '../workspace-state'

const request = vi.hoisted(() => vi.fn())

// Keep the generated router, transport decoding, query, normalizer and atoms real.
vi.mock('@/service/base', () => ({ request }))

const createSummary = (
  overrides: Partial<GetWorkspacesCurrentSummaryResponse> = {},
): GetWorkspacesCurrentSummaryResponse => ({
  id: 'workspace-1',
  name: 'Workspace',
  plan: null,
  credits: null,
  role: 'normal',
  admin_extend: false,
  tenant_extend: false,
  ...overrides,
})

const cleanups: Array<() => void> = []

function subscribeToWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createStore()
  store.set(queryClientAtom, queryClient)
  const unsubscribe = store.sub(currentWorkspaceAtom, () => {})
  cleanups.push(() => {
    unsubscribe()
    queryClient.clear()
  })
  return { queryClient, store }
}

describe('Workspace summary fork permissions', () => {
  beforeEach(() => {
    request.mockReset()
  })

  afterEach(() => {
    cleanups.splice(0).forEach((cleanup) => cleanup())
  })

  it.each([
    ['owner', true, true, true, true],
    ['admin', false, true, false, true],
    ['editor', true, false, false, false],
    ['normal', false, false, false, false],
    ['dataset_operator', true, true, false, false],
  ] as const)(
    'preserves summary bits for %s without changing upstream role grants',
    async (role, adminExtend, tenantExtend, owner, manager) => {
      request.mockResolvedValue(
        Response.json(
          createSummary({
            role,
            admin_extend: adminExtend,
            tenant_extend: tenantExtend,
          }),
        ),
      )
      const { store } = subscribeToWorkspace()

      await vi.waitFor(() => expect(store.get(currentWorkspaceAtom).id).toBe('workspace-1'))

      expect(store.get(adminExtendAtom)).toBe(adminExtend)
      expect(store.get(tenantExtendAtom)).toBe(tenantExtend)
      expect(store.get(isCurrentWorkspaceOwnerAtom)).toBe(owner)
      expect(store.get(isCurrentWorkspaceManagerAtom)).toBe(manager)
      expect(request.mock.calls[0]?.[0]).toMatch(/\/workspaces\/current\/summary$/)
    },
  )

  it('uses no fork permissions while the summary request is pending', () => {
    request.mockImplementation(() => new Promise<Response>(() => {}))
    const { store } = subscribeToWorkspace()

    expect(store.get(currentWorkspaceLoadingAtom)).toBe(true)
    expect(store.get(adminExtendAtom)).toBe(false)
    expect(store.get(tenantExtendAtom)).toBe(false)
  })

  it('updates both permission bits when the authenticated summary is refreshed', async () => {
    request.mockResolvedValueOnce(
      Response.json(
        createSummary({
          admin_extend: true,
          tenant_extend: true,
        }),
      ),
    )
    const { queryClient, store } = subscribeToWorkspace()
    await vi.waitFor(() => expect(store.get(adminExtendAtom)).toBe(true))

    request.mockResolvedValueOnce(Response.json(createSummary({ id: 'workspace-2' })))
    await queryClient.invalidateQueries({
      queryKey: consoleQuery.workspaces.current.summary.get.key(),
    })

    await vi.waitFor(() => expect(store.get(currentWorkspaceAtom).id).toBe('workspace-2'))
    expect(store.get(adminExtendAtom)).toBe(false)
    expect(store.get(tenantExtendAtom)).toBe(false)
  })

  it('starts without the previous identity permissions in a fresh session cache', async () => {
    request.mockResolvedValueOnce(
      Response.json(
        createSummary({
          admin_extend: true,
          tenant_extend: true,
        }),
      ),
    )
    const previousSession = subscribeToWorkspace()
    await vi.waitFor(() => expect(previousSession.store.get(adminExtendAtom)).toBe(true))

    // Workspace switching reloads the document; logout clears the QueryClient.
    request.mockImplementation(() => new Promise<Response>(() => {}))
    const nextSession = subscribeToWorkspace()
    expect(nextSession.store.get(adminExtendAtom)).toBe(false)
    expect(nextSession.store.get(tenantExtendAtom)).toBe(false)
  })

  it.each(['admin_extend', 'tenant_extend'] as const)(
    'rejects a response missing %s instead of treating it as a successful denial',
    async (field) => {
      const summary = createSummary({ [field]: undefined })
      request.mockResolvedValue(Response.json(summary))
      const { queryClient, store } = subscribeToWorkspace()

      await vi.waitFor(() => expect(store.get(currentWorkspaceLoadingAtom)).toBe(false))

      const response = queryClient.getQueryData<GetWorkspacesCurrentSummaryResponse>(
        consoleQuery.workspaces.current.summary.get.queryKey(),
      )
      expect(() => normalizeCurrentWorkspaceSummary(response)).toThrow()
      expect(store.get(currentWorkspaceAtom).id).toBe('')
      expect(store.get(adminExtendAtom)).toBe(false)
      expect(store.get(tenantExtendAtom)).toBe(false)
    },
  )

  it('preserves an archived workspace rejection in query state', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    request.mockResolvedValue(
      Response.json(
        { code: 'current_workspace_archived', message: 'Current workspace is archived' },
        { status: 409 },
      ),
    )
    const { queryClient, store } = subscribeToWorkspace()

    try {
      await vi.waitFor(() => expect(store.get(currentWorkspaceLoadingAtom)).toBe(false))
      expect(
        queryClient.getQueryState(consoleQuery.workspaces.current.summary.get.queryKey()),
      ).toMatchObject({ status: 'error', error: { status: 409 } })
      expect(store.get(adminExtendAtom)).toBe(false)
      expect(store.get(tenantExtendAtom)).toBe(false)
    } finally {
      errorLog.mockRestore()
    }
  })
})
