import type { ChatConfig } from '@/app/components/base/chat/types'
import type { ExploreAppDetailResponse } from '@/contract/console/explore'
import type { AppMeta } from '@/models/share'
import { consoleClient } from './client'

export const fetchAppList = (language?: string) => {
  if (!language)
    return consoleClient.explore.apps({})

  return consoleClient.explore.apps({
    query: { language },
  })
}

// -------------- extend: start fetch Open Installed App List ---------------
export const fetchOpenInstalledAppList = () => {
  return get<{
    categories: AppCategory[]
    recommended_apps: App[]
  }>('/installed/apps')
}
// -------------- extend: stop fetch Open Installed App List ---------------

// eslint-disable-next-line ts/no-explicit-any
export const fetchAppDetail = (id: string): Promise<any> => {
  return get(`/explore/apps/${id}`)
}

export const fetchInstalledAppList = (app_id?: string | null) => {
  return get<{
    installed_apps: InstalledApp[]
  }>(`/installed-apps${app_id ? `?app_id=${app_id}` : ''}`)
}

export const uninstallApp = (id: string) => {
  return consoleClient.explore.uninstallInstalledApp({
    params: { id },
  })
}

export const updatePinStatus = (id: string, isPinned: boolean) => {
  return consoleClient.explore.updateInstalledApp({
    params: { id },
    body: {
      is_pinned: isPinned,
    },
  })
}

export const getAppAccessModeByAppId = (appId: string) => {
  return consoleClient.explore.appAccessMode({
    query: { appId },
  })
}

export const fetchInstalledAppParams = (appId: string) => {
  return consoleClient.explore.installedAppParameters({
    params: { appId },
  }) as Promise<ChatConfig>
}

export const fetchInstalledAppMeta = (appId: string) => {
  return consoleClient.explore.installedAppMeta({
    params: { appId },
  }) as Promise<AppMeta>
}

export const fetchBanners = (language?: string) => {
  if (!language)
    return consoleClient.explore.banners({})

  return consoleClient.explore.banners({
    query: { language },
  })
}
