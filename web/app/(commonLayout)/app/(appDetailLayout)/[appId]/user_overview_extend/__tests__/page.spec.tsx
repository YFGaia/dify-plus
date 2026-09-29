import type { App } from '@/types/app'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Suspense } from 'react'
import { useStore } from '@/app/components/app/store'
import { renderWithConsoleQuery } from '@/test/console/query-data'
import { AppModeEnum } from '@/types/app'
import { AppACLPermission } from '@/utils/permission'
import UserOverView from '../page'

vi.hoisted(() => {
  vi.setSystemTime(new Date('2026-09-29T12:00:00Z'))
})
afterAll(() => vi.useRealTimers())
const mockRequest = vi.hoisted(() => vi.fn<(url: string) => Promise<Response>>())
const mockConsoleState = vi.hoisted(() => ({
  currentWorkspace: { id: 'workspace-1' },
  isLoadingCurrentWorkspace: false,
  isLoadingWorkspacePermissionKeys: false,
  workspacePermissionKeys: [] as string[],
}))

vi.mock('@/context/workspace-state', async () => {
  const { createWorkspaceStateModuleMock } = await import('@/test/console/state-fixture')
  return createWorkspaceStateModuleMock(() => mockConsoleState)
})
vi.mock('@/context/permission-state', async () => {
  const { createPermissionStateModuleMock } = await import('@/test/console/state-fixture')
  return createPermissionStateModuleMock(() => mockConsoleState)
})
vi.mock('@/service/base', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/service/base')>()),
  request: mockRequest,
}))
vi.mock('echarts-for-react', () => ({
  default: () => <div role="img" aria-label="Usage chart" />,
}))

const appDetail = (overrides: Partial<App> = {}) =>
  ({
    id: 'app-1',
    name: 'Demo App',
    mode: AppModeEnum.CHAT,
    permission_keys: [AppACLPermission.Monitor],
    ...overrides,
  }) as App
const page = (params = Promise.resolve({ appId: 'app-1' })) => (
  <Suspense fallback={<div>Loading route</div>}>
    <UserOverView params={params} />
  </Suspense>
)
const renderPage = async () => {
  const rendered: { current?: ReturnType<typeof renderWithConsoleQuery> } = {}
  await act(async () => {
    rendered.current = renderWithConsoleQuery(page())
  })
  if (!rendered.current) throw new Error('Page did not render')
  return rendered.current
}
const urls = () => mockRequest.mock.calls.map(([url]) => new URL(url, 'http://localhost'))

const chartCases = [
  {
    mode: AppModeEnum.CHAT,
    paths: [
      '/apps/app-1/statistics/daily-conversations',
      '/apps/app-1/statistics/average-session-interactions',
      '/apps/app-1/statistics/token-costs',
    ],
  },
  {
    mode: AppModeEnum.WORKFLOW,
    paths: [
      '/apps/app-1/workflow/statistics/daily-conversations',
      '/apps/app-1/workflow/statistics/token-costs',
      '/apps/app-1/workflow/statistics/average-app-interactions',
    ],
  },
]

describe('User overview', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockConsoleState.currentWorkspace = { id: 'workspace-1' }
    mockConsoleState.isLoadingCurrentWorkspace = false
    mockConsoleState.isLoadingWorkspacePermissionKeys = false
    mockConsoleState.workspacePermissionKeys = []
    mockRequest.mockImplementation(
      async () =>
        new Response(JSON.stringify({ data: [] }), {
          headers: { 'Content-Type': 'application/json' },
        }),
    )
    useStore.getState().setAppDetail(appDetail())
  })

  it.each(
    chartCases.flatMap(({ mode, paths }) =>
      [
        'allTime',
        'today',
        'last4weeks',
        'last3months',
        'last12months',
        'monthToDate',
        'quarterToDate',
        'yearToDate',
      ].map((period) => ({ mode, paths, period })),
    ),
  )(
    'unwraps Promise params and keeps account filtering for $mode / $period requests',
    async ({ mode, paths, period }) => {
      const user = userEvent.setup()
      useStore.getState().setAppDetail(appDetail({ mode }))
      await renderPage()
      await waitFor(() =>
        expect(screen.getAllByRole('img', { name: 'Usage chart' })).toHaveLength(3),
      )
      expect(urls()).toHaveLength(3)
      for (const url of urls()) {
        expect(paths.some((path) => url.pathname.endsWith(path))).toBe(true)
        expect(url.searchParams.get('account')).toBe('true')
        expect(url.searchParams.get('start')).toBeTruthy()
        expect(url.searchParams.get('end')).toBeTruthy()
      }

      mockRequest.mockClear()
      await user.click(screen.getByRole('combobox'))
      await user.click(
        await screen.findByRole('option', { name: `appLog.filter.period.${period}` }),
      )
      await waitFor(() => expect(mockRequest).toHaveBeenCalledTimes(3))
      for (const url of urls()) {
        expect(paths.some((path) => url.pathname.endsWith(path))).toBe(true)
        expect(url.searchParams.get('account')).toBe('true')
        expect(url.searchParams.has('start')).toBe(period !== 'allTime')
        expect(url.searchParams.has('end')).toBe(period !== 'allTime')
      }
    },
  )

  it.each(['denied', 'permissions loading', 'workspace loading', 'different app'])(
    'does not mount charts or request statistics with %s',
    async (state) => {
      if (state === 'denied')
        useStore
          .getState()
          .setAppDetail(appDetail({ permission_keys: [AppACLPermission.ViewLayout] }))
      if (state === 'permissions loading') mockConsoleState.isLoadingWorkspacePermissionKeys = true
      if (state === 'workspace loading') mockConsoleState.isLoadingCurrentWorkspace = true
      if (state === 'different app') useStore.getState().setAppDetail(appDetail({ id: 'app-2' }))
      await renderPage()
      expect(screen.queryByText('Loading route')).not.toBeInTheDocument()
      expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
      expect(screen.queryByRole('img')).not.toBeInTheDocument()
      expect(mockRequest).not.toHaveBeenCalled()
    },
  )

  it('removes charts when monitor access is revoked', async () => {
    await renderPage()
    await screen.findAllByRole('img', { name: 'Usage chart' })
    mockRequest.mockClear()
    act(() => useStore.getState().setAppDetail(appDetail({ permission_keys: [] })))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(mockRequest).not.toHaveBeenCalled()
  })

  it('does not reuse the previous app permission while the route switches', async () => {
    const { rerender } = await renderPage()
    await screen.findAllByRole('img', { name: 'Usage chart' })
    mockRequest.mockClear()
    await act(async () => rerender(page(Promise.resolve({ appId: 'app-2' }))))
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(mockRequest).not.toHaveBeenCalled()
  })
})
