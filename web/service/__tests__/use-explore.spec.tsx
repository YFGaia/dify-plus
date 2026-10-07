import type { ReactNode } from 'react'
import type { App } from '@/models/explore'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { consoleQuery } from '@/service/console'
import { AppModeEnum } from '@/types/app'
import { fetchAppList } from '../explore'
import { useExploreAppList, useInstalledAppList } from '../use-explore'

vi.mock('../explore', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../explore')>()),
  fetchAppList: vi.fn(),
}))

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  })

  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

const createApp = (appId: string, position: number): App => ({
  app: {
    id: appId,
    mode: AppModeEnum.CHAT,
    icon_type: 'emoji',
    icon: 'robot',
    icon_background: '#fff',
    icon_url: '',
    name: appId,
    description: '',
    use_icon_as_answer_icon: false,
  },
  app_id: appId,
  description: '',
  copyright: '',
  privacy_policy: null,
  custom_disclaimer: null,
  categories: [],
  position,
  is_listed: true,
  install_count: 0,
  installed: false,
  editable: false,
  is_agent: false,
  can_trial: false,
})

describe('useExploreAppList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(fetchAppList).mockResolvedValue({
      categories: [],
      recommended_apps: [createApp('app-2', 2), createApp('app-1', 1)],
    })
  })

  // Explore app list can now be disabled by callers.
  describe('Queries', () => {
    it('should not fetch app list when disabled', () => {
      renderHook(() => useExploreAppList({ enabled: false }), { wrapper: createWrapper() })

      expect(fetchAppList).not.toHaveBeenCalled()
    })

    it('should fetch localized app list and sort recommended apps by position', async () => {
      const { result } = renderHook(() => useExploreAppList({ enabled: true }), {
        wrapper: createWrapper(),
      })

      await waitFor(() => {
        expect(fetchAppList).toHaveBeenCalledWith('en-US')
      })
      await waitFor(() => {
        expect(result.current.data?.allList.map((app) => app.app_id)).toEqual(['app-1', 'app-2'])
      })
    })
  })
})

describe('useInstalledAppList cache boundary', () => {
  afterEach(() => vi.unstubAllGlobals())

  const setup = (response: Record<string, unknown>) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    return { client, ...renderHook(() => useInstalledAppList(), { wrapper }) }
  }

  it('preserves backend usage order and validates before storing the adapted result', async () => {
    const rows = ['most-used', 'less-used'].map((id, index) => ({
      app_id: id,
      installed_id: `installed-${id}`,
      category: 'Writing',
      description: '',
      app: { id, name: id, mode: 'chat' },
      position: 10 - index,
    }))
    const { client, result } = setup({ categories: ['Writing'], recommended_apps: rows })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.allList.map((app) => app.app_id)).toEqual([
      'most-used',
      'less-used',
    ])
    expect(
      client.getQueryData([...consoleQuery.installed.apps.get.queryKey(), 'app-center']),
    ).toEqual(result.current.data)
    expect(client.getQueryData(consoleQuery.installed.apps.get.queryKey())).toBeUndefined()
  })

  it('rejects malformed rows before they enter the raw query cache', async () => {
    const { client, result } = setup({ categories: [], recommended_apps: [{ app_id: 'broken' }] })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.data).toBeUndefined()
    const queries = client.getQueryCache().getAll()
    expect(queries).toHaveLength(1)
    expect(queries[0]?.state.data).toBeUndefined()
    expect(queries[0]?.state.status).toBe('error')
  })
})
