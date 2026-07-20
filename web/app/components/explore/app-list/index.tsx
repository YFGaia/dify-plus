'use client'

import type { CreateAppModalProps } from '@/app/components/explore/create-app-modal'
import type { Banner as BannerType } from '@/models/app'
import type { App } from '@/models/explore'
import type { App as WorkspaceApp } from '@/types/app'
import type { TryAppSelection } from '@/types/try-app'
import type { TrackCreateAppParams } from '@/utils/create-app-tracking'
import { cn } from '@langgenius/dify-ui/cn'
import { queryOptions, useQueries, useQueryClient, useSuspenseQuery } from '@tanstack/react-query'
import { useDebounceFn } from 'ahooks'
import { useAtomValue } from 'jotai'
import { useQueryState } from 'nuqs'
import * as React from 'react'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import DSLConfirmModal from '@/app/components/app/create-from-dsl-modal/dsl-confirm-modal'
import AppCard from '@/app/components/explore/app-card'
import { Banner } from '@/app/components/explore/banner/banner'
import CreateAppModal from '@/app/components/explore/create-app-modal'
import { useLocale } from '@/context/i18n'
import { workspacePermissionKeysAtom } from '@/context/permission-state'
import { systemFeaturesQueryOptions } from '@/features/system-features/client'
// Extend: start Explore Add Search
import { TagFilter } from '@/features/tag-management/components/tag-filter'
// Extend: stop Explore Add Search
import { useImportDSL } from '@/hooks/use-import-dsl'
import { DSLImportMode } from '@/models/app'
import dynamic from '@/next/dynamic'
import { consoleQuery } from '@/service/client'
import { fetchAppDetail, fetchAppList, fetchBanners } from '@/service/explore'
import { normalizeAppPagination } from '@/service/use-apps'
import { trackCreateApp } from '@/utils/create-app-tracking'
import { hasPermission } from '@/utils/permission'
import { ExploreAppListHeader } from './explore-app-list-header'
import { ExploreRecommendations } from './explore-recommendations'
import { ExploreHomeSkeleton } from './loading-skeletons'
import s from './style.module.css'

const TryApp = dynamic(() => import('../try-app'), { ssr: false })

type ExploreAppListData = {
  categories: string[]
  allList: App[]
}

const homeContinueWorkAppsInput = {
  query: {
    page: 1,
    limit: 8,
    name: '',
  },
}

const disabledBannersQueryKey = ['explore', 'home', 'banners', 'disabled'] as const

function getLocaleQueryInput(locale?: string) {
  return locale ? { query: { language: locale } } : {}
}

function getExploreAppListQueryOptions(locale?: string) {
  const input = getLocaleQueryInput(locale)
  const language = input.query?.language

  return queryOptions<ExploreAppListData>({
    queryKey: [...consoleQuery.explore.apps.get.queryKey({ input }), language],
    queryFn: async () => {
      const { categories, recommended_apps } = await fetchAppList(language)
      return {
        categories,
        allList: [...recommended_apps].sort((a, b) => a.position - b.position),
      }
    },
  })
}

function getContinueWorkAppsQueryOptions() {
  return consoleQuery.apps.get.queryOptions({
    input: homeContinueWorkAppsInput,
    select: (response): WorkspaceApp[] => normalizeAppPagination(response).data,
  })
}

function getBannersQueryOptions(locale?: string) {
  const input = getLocaleQueryInput(locale)
  const language = input.query?.language

  return queryOptions<BannerType[]>({
    queryKey: [...consoleQuery.explore.banners.get.queryKey({ input }), language],
    queryFn: () => fetchBanners(language),
  })
}

function getDisabledBannersQueryOptions() {
  return queryOptions<BannerType[]>({
    queryKey: disabledBannersQueryKey,
    queryFn: async () => [],
    initialData: [],
    staleTime: 'static',
  })
}

const Apps = ({ onSuccess }: { onSuccess?: () => void }) => {
  const { t } = useTranslation()
  const locale = useLocale()
  const workspacePermissionKeys = useAtomValue(workspacePermissionKeysAtom)
  const { data: systemFeatures } = useSuspenseQuery(systemFeaturesQueryOptions())
  const homeQueries = useQueries({
    queries: [
      getExploreAppListQueryOptions(locale),
      getContinueWorkAppsQueryOptions(),
      systemFeatures.enable_explore_banner
        ? getBannersQueryOptions(locale)
        : getDisabledBannersQueryOptions(),
    ],
    combine: ([exploreAppListQuery, continueWorkAppsQuery, bannersQuery]) => ({
      appListData: exploreAppListQuery.data,
      continueWorkApps: continueWorkAppsQuery.data ?? [],
      banners: bannersQuery.data ?? [],
      isPending:
        exploreAppListQuery.isPending || continueWorkAppsQuery.isPending || bannersQuery.isPending,
      isAppListError:
        exploreAppListQuery.isError ||
        (!exploreAppListQuery.isPending && !exploreAppListQuery.data),
    }),
  })
  const allCategoriesEn = t(($) => $['apps.allCategories'], { ns: 'explore', lng: 'en' })
  const canCreateApp = hasPermission(workspacePermissionKeys, 'app.create_and_management')

  const [keywords, setKeywords] = useState('')
  const [searchKeywords, setSearchKeywords] = useState('')

  const { run: handleSearch } = useDebounceFn(
    () => {
      setSearchKeywords(keywords)
    },
    { wait: 500 },
  )

  const handleKeywordsChange = (value: string) => {
    setKeywords(value)
    handleSearch()
  }

  const [currCategory, setCurrCategory] = useQueryState('category', {
    defaultValue: allCategoriesEn,
  })

  // Extend: start Explore Add Search
  const [tagFilterValue, setTagFilterValue] = useState<string[]>([])
  const handleTagsChange = (value: string[]) => {
    setTagFilterValue(value)
  }
  // Extend: stop Explore Add Search

  const visibleCategories = useMemo(() => {
    if (!homeQueries.appListData) return []

    const categoriesWithApps = new Set<string>()
    homeQueries.appListData.allList.forEach((app) => {
      app.categories.forEach((category) => categoriesWithApps.add(category))
    })

    return homeQueries.appListData.categories.filter((category) => categoriesWithApps.has(category))
  }, [homeQueries.appListData])

  const activeCategory = visibleCategories.includes(currCategory) ? currCategory : allCategoriesEn

  // extend: start sync app — 推荐列表中的 app_id 集合，用于判断是否已同步
  const recommendedAppIds = useMemo(() => {
    if (!homeQueries.appListData) return new Set<string>()
    return new Set(homeQueries.appListData.allList.map((item) => item.app_id))
  }, [homeQueries.appListData])
  const queryClient = useQueryClient()
  const refreshExploreAppList = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: consoleQuery.explore.apps.key() })
  }, [queryClient])
  // extend: stop sync app

  const filteredList = useMemo(() => {
    if (!homeQueries.appListData) return []
    let result = homeQueries.appListData.allList.filter(
      (item) => activeCategory === allCategoriesEn || item.categories?.includes(activeCategory),
    )
    // Extend: start Explore Add Search — 标签过滤（基于分类名）
    if (tagFilterValue.length > 0)
      result = result.filter((item) =>
        item.categories?.some((category) => tagFilterValue.includes(category)),
      )
    // Extend: stop Explore Add Search
    return result
  }, [homeQueries.appListData, activeCategory, allCategoriesEn, tagFilterValue])

  const searchFilteredList = useMemo(() => {
    if (!searchKeywords || !filteredList || filteredList.length === 0) return filteredList

    const lowerCaseSearchKeywords = searchKeywords.toLowerCase()

    // Extend: 搜索同时匹配应用名与描述
    return filteredList.filter(
      (item) =>
        (item.app &&
          item.app.name &&
          item.app.name.toLowerCase().includes(lowerCaseSearchKeywords)) ||
        (typeof item.description === 'string' &&
          item.description.toLowerCase().includes(lowerCaseSearchKeywords)),
    )
  }, [searchKeywords, filteredList])

  const [currApp, setCurrApp] = useState<App | null>(null)
  const [isShowCreateModal, setIsShowCreateModal] = useState(false)

  const { handleImportDSL, handleImportDSLConfirm, versions, isFetching } = useImportDSL()
  const [showDSLConfirmModal, setShowDSLConfirmModal] = useState(false)

  const [currentTryApp, setCurrentTryApp] = useState<TryAppSelection | undefined>(undefined)
  const currentCreateAppModeRef = useRef<App['app']['mode'] | null>(null)
  const currentCreateAppTrackingRef = useRef<Pick<
    TrackCreateAppParams,
    'source' | 'templateId'
  > | null>(null)
  const isShowTryAppPanel = !!currentTryApp
  const hideTryAppPanel = useCallback(() => {
    setCurrentTryApp(undefined)
  }, [])
  const handleTryApp = useCallback((params: TryAppSelection) => {
    setCurrentTryApp(params)
  }, [])
  const handleShowFromTryApp = useCallback(() => {
    setCurrApp(currentTryApp?.app || null)
    currentCreateAppTrackingRef.current = {
      source: 'explore_template_preview',
      templateId: currentTryApp?.appId || currentTryApp?.app.app_id,
    }
    setIsShowCreateModal(true)
  }, [currentTryApp?.app, currentTryApp?.appId])
  const handleCreateFromLearnDify = useCallback((app: App) => {
    setCurrApp(app)
    setIsShowCreateModal(true)
  }, [])
  const handleCreateFromAppList = useCallback((app: App) => {
    currentCreateAppTrackingRef.current = {
      source: 'explore_template_list',
      templateId: app.app_id,
    }
    setCurrApp(app)
    setIsShowCreateModal(true)
  }, [])
  const trackCurrentCreateApp = useCallback((appMode?: App['app']['mode'] | null) => {
    const currentCreateAppTracking = currentCreateAppTrackingRef.current
    const resolvedAppMode = appMode ?? currentCreateAppModeRef.current
    if (!resolvedAppMode || !currentCreateAppTracking) return

    trackCreateApp({
      ...currentCreateAppTracking,
      appMode: resolvedAppMode,
    })
    currentCreateAppTrackingRef.current = null
    currentCreateAppModeRef.current = null
  }, [])

  const onCreate: CreateAppModalProps['onConfirm'] = useCallback(
    async ({ name, icon_type, icon, icon_background, description }) => {
      hideTryAppPanel()

      const appId = currApp?.app.id
      if (!appId) return

      const { export_data, mode } = await fetchAppDetail(appId)
      currentCreateAppModeRef.current = mode
      const payload = {
        mode: DSLImportMode.YAML_CONTENT,
        yaml_content: export_data,
        name,
        icon_type,
        icon,
        icon_background,
        description,
      }
      await handleImportDSL(payload, {
        onSuccess: (response) => {
          trackCurrentCreateApp(response.app_mode)
          setIsShowCreateModal(false)
        },
        onPending: () => {
          setShowDSLConfirmModal(true)
        },
      })
    },
    [currApp?.app.id, handleImportDSL, hideTryAppPanel, trackCurrentCreateApp],
  )

  const onConfirmDSL = useCallback(async () => {
    await handleImportDSLConfirm({
      onSuccess: (response) => {
        trackCurrentCreateApp(response.app_mode)
        onSuccess?.()
      },
    })
  }, [handleImportDSLConfirm, onSuccess, trackCurrentCreateApp])

  if (homeQueries.isAppListError) return null

  return (
    <div
      className={cn(
        'flex h-full min-h-0 flex-col overflow-hidden border-l-[0.5px] border-divider-regular',
      )}
    >
      <div className="flex flex-1 flex-col overflow-y-auto">
        {homeQueries.isPending ? (
          <ExploreHomeSkeleton showBanner={systemFeatures.enable_explore_banner} />
        ) : (
          <>
            {systemFeatures.enable_explore_banner && <Banner banners={homeQueries.banners} />}
            <ExploreRecommendations
              canCreate={canCreateApp}
              continueWorkApps={homeQueries.continueWorkApps}
              onCreate={handleCreateFromLearnDify}
              onTry={handleTryApp}
            />

            <ExploreAppListHeader
              allCategoriesEn={allCategoriesEn}
              categories={visibleCategories}
              currCategory={activeCategory}
              keywords={keywords}
              onCategoryChange={setCurrCategory}
              onKeywordsChange={handleKeywordsChange}
            />
            {/* Extend: start Explore Add Search — 标签过滤 */}
            <div className="flex items-center justify-end px-8 pb-3">
              <TagFilter type="app" value={tagFilterValue} onChange={handleTagsChange} />
            </div>
            {/* Extend: stop Explore Add Search */}

            <div className={cn('relative flex flex-1 shrink-0 grow flex-col pb-6')}>
              <nav className={cn(s.appList, 'grid shrink-0 content-start gap-3 px-8')}>
                {searchFilteredList.map((app) => (
                  <AppCard
                    key={app.app_id}
                    app={app}
                    canCreate={canCreateApp}
                    onCreate={() => handleCreateFromAppList(app)}
                    onTry={handleTryApp}
                    // extend: start sync app
                    onApp={recommendedAppIds.has(app.app_id)}
                    onRefresh={refreshExploreAppList}
                    // extend: stop sync app
                  />
                ))}
              </nav>
            </div>
          </>
        )}
      </div>
      {isShowCreateModal && (
        <CreateAppModal
          appIconType={currApp?.app.icon_type || 'emoji'}
          appIcon={currApp?.app.icon || ''}
          appIconBackground={currApp?.app.icon_background || ''}
          appIconUrl={currApp?.app.icon_url}
          appName={currApp?.app.name || ''}
          appDescription={currApp?.app.description || ''}
          show={isShowCreateModal}
          onConfirm={onCreate}
          confirmDisabled={isFetching}
          onHide={() => setIsShowCreateModal(false)}
        />
      )}
      {showDSLConfirmModal && (
        <DSLConfirmModal
          versions={versions}
          onCancel={() => setShowDSLConfirmModal(false)}
          onConfirm={onConfirmDSL}
          confirmDisabled={isFetching}
        />
      )}

      {isShowTryAppPanel && (
        <TryApp
          appId={currentTryApp?.appId || ''}
          app={currentTryApp?.app}
          categories={currentTryApp?.app?.categories}
          onClose={hideTryAppPanel}
          onCreate={handleShowFromTryApp}
        />
      )}
    </div>
  )
}

export default React.memo(Apps)
