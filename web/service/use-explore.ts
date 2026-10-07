import type { App, AppCategory } from '@/models/explore'
import { queryOptions, useQuery } from '@tanstack/react-query'
import { useLocale } from '@/context/i18n'
import { consoleQuery } from '@/service/console'
import { fetchAppList, fetchLearnDifyAppList, fetchOpenInstalledAppList } from './explore'

export const useInstalledAppList = () =>
  useQuery(
    queryOptions({
      // The app center caches validated, adapted rows, not the generated raw record.
      queryKey: [...consoleQuery.installed.apps.get.queryKey(), 'app-center'],
      queryFn: async () => {
        const { categories, recommended_apps } = await fetchOpenInstalledAppList()
        // The backend orders by usage; position is unrelated to that order.
        return { categories, allList: recommended_apps }
      },
    }),
  )

type ExploreAppListData = {
  categories: AppCategory[]
  allList: App[]
}

export const useExploreAppList = (options: { enabled?: boolean } = {}) => {
  const locale = useLocale()
  const exploreAppsInput = locale ? { query: { language: locale } } : {}
  const exploreAppsLanguage = exploreAppsInput?.query?.language

  return useQuery<ExploreAppListData>({
    queryKey: [
      ...consoleQuery.explore.apps.get.queryKey({ input: exploreAppsInput }),
      exploreAppsLanguage,
    ],
    queryFn: async () => {
      const { categories, recommended_apps } = await fetchAppList(exploreAppsLanguage)
      return {
        categories,
        allList: [...recommended_apps].sort((a, b) => a.position - b.position),
      }
    },
    enabled: options.enabled,
  })
}

export const useLearnDifyAppList = () => {
  const locale = useLocale()
  const learnDifyAppsInput = locale ? { query: { language: locale } } : {}
  const learnDifyAppsLanguage = learnDifyAppsInput?.query?.language

  return useQuery({
    queryKey: [
      ...consoleQuery.explore.apps.learnDify.get.queryKey({ input: learnDifyAppsInput }),
      learnDifyAppsLanguage,
    ],
    queryFn: async () => {
      const { recommended_apps } = await fetchLearnDifyAppList(learnDifyAppsLanguage)
      return [...recommended_apps].sort((a, b) => a.position - b.position)
    },
  })
}
