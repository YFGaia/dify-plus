import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import {
  fetchAppDetail,
  fetchAppList,
  fetchInstalledAppList,
  fetchOpenInstalledAppList,
} from './explore'

const mockExploreAppsGet = vi.hoisted(() => vi.fn())
const mockExploreAppDetailGet = vi.hoisted(() => vi.fn())
const mockOpenInstalledAppsGet = vi.hoisted(() => vi.fn())
const mockInstalledAppsGet = vi.hoisted(() => vi.fn())

vi.mock('@/service/console', () => ({
  consoleClient: {
    explore: {
      apps: {
        get: mockExploreAppsGet,
        byAppId: {
          get: mockExploreAppDetailGet,
        },
      },
    },
    installed: { apps: { get: mockOpenInstalledAppsGet } },
    installedApps: {
      get: mockInstalledAppsGet,
    },
  },
}))

describe('explore service normalizers', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('preserves backend app modes that are not part of the legacy frontend enum', async () => {
    mockExploreAppsGet.mockResolvedValue({
      categories: [],
      recommended_apps: [
        {
          app_id: 'agent-app',
          app: {
            id: 'agent-app',
            name: 'Agent app',
            mode: 'agent',
            icon: '',
            icon_background: '',
          },
        },
      ],
    })
    mockExploreAppDetailGet.mockResolvedValue({
      id: 'pipeline-app',
      name: 'Pipeline app',
      icon: '',
      icon_background: '',
      mode: 'rag-pipeline',
      export_data: 'kind: app',
      can_trial: false,
    })

    await expect(fetchAppList()).resolves.toMatchObject({
      recommended_apps: [
        {
          app: {
            mode: 'agent',
          },
        },
      ],
    })
    await expect(fetchAppDetail('pipeline-app')).resolves.toMatchObject({
      mode: 'rag-pipeline',
    })
  })

  it('preserves installed app pagination metadata', async () => {
    mockInstalledAppsGet.mockResolvedValue({
      installed_apps: [],
      has_more: true,
      next_cursor: 'next-page',
    })

    await expect(fetchInstalledAppList()).resolves.toEqual({
      installed_apps: [],
      has_more: true,
      next_cursor: 'next-page',
    })
  })
})

const installedRow = {
  app_id: 'source-app',
  installed_id: 'installed-app',
  category: 'Writing',
  description: 'Write reports',
  app: { id: 'installed-app', name: 'Writer', mode: 'chat', icon_type: 'emoji' },
}

describe('fork installed app response', () => {
  beforeEach(() => vi.resetAllMocks())

  it('adapts singular categories and nullable display fields while preserving installed identity', async () => {
    mockOpenInstalledAppsGet.mockResolvedValue({
      categories: ['Writing'],
      recommended_apps: [
        { ...installedRow, description: null, app: { ...installedRow.app, icon: null } },
      ],
    })
    await expect(fetchOpenInstalledAppList()).resolves.toMatchObject({
      categories: ['Writing'],
      recommended_apps: [
        {
          app_id: 'source-app',
          installed_id: 'installed-app',
          category: 'Writing',
          categories: ['Writing'],
          description: '',
          app: { id: 'installed-app', icon: '' },
        },
      ],
    })
    expect(mockOpenInstalledAppsGet).toHaveBeenCalledOnce()
    expect(mockInstalledAppsGet).not.toHaveBeenCalled()
  })

  it.each([
    { installed_apps: [] },
    { categories: [], recommended_apps: null },
    { categories: [42], recommended_apps: [] },
    ...[
      { app_id: '' },
      { installed_id: '  ' },
      { category: ['Writing'] },
      { app: null },
      { app: { ...installedRow.app, name: 123 } },
      { description: {} },
      { position: '1' },
    ].map((invalid) => ({ categories: [], recommended_apps: [{ ...installedRow, ...invalid }] })),
  ])('rejects malformed installed data: %j', async (response) => {
    mockOpenInstalledAppsGet.mockResolvedValue(response)
    await expect(fetchOpenInstalledAppList()).rejects.toThrow()
  })

  it('propagates API failures', async () => {
    mockOpenInstalledAppsGet.mockRejectedValue(new Error('Service unavailable'))
    await expect(fetchOpenInstalledAppList()).rejects.toThrow('Service unavailable')
  })
})
