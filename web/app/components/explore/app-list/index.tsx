'use client'

import type { CreateAppModalProps } from '@/app/components/explore/create-app-modal'
import type { App } from '@/models/explore'
import type { TryAppSelection } from '@/types/try-app'
import type { TrackCreateAppParams } from '@/utils/create-app-tracking'
import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { useSuspenseQuery } from '@tanstack/react-query'
import { useDebounceFn } from 'ahooks'
import { useQueryState } from 'nuqs'
import * as React from 'react'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import DSLConfirmModal from '@/app/components/app/create-from-dsl-modal/dsl-confirm-modal'
import Input from '@/app/components/base/input'
import Loading from '@/app/components/base/loading'
import AppCard from '@/app/components/explore/app-card'
// Extend: stop Explore Add Search
import Banner from '@/app/components/explore/banner/banner'
import Category from '@/app/components/explore/category'
import CreateAppModal from '@/app/components/explore/create-app-modal'
import { useAppContext } from '@/context/app-context'
// Extend: start Explore Add Search
import { TagFilter } from '@/features/tag-management/components/tag-filter'
import { useImportDSL } from '@/hooks/use-import-dsl'
import {
  DSLImportMode,
} from '@/models/app'
import { fetchAppDetail } from '@/service/explore'
import { systemFeaturesQueryOptions } from '@/service/system-features'
import { useMembers } from '@/service/use-common'
import { useExploreAppList } from '@/service/use-explore'
import { trackCreateApp } from '@/utils/create-app-tracking'
import TryApp from '../try-app'
import s from './style.module.css'

type AppsProps = {
  onSuccess?: () => void
}

const Apps = ({
  onSuccess,
}: AppsProps) => {
  const { t } = useTranslation()
  const { userProfile } = useAppContext()
  const { data: systemFeatures } = useSuspenseQuery(systemFeaturesQueryOptions())
  const { data: membersData } = useMembers()
  const allCategoriesEn = t('apps.allCategories', { ns: 'explore', lng: 'en' })
  const userAccount = membersData?.accounts?.find(account => account.id === userProfile.id)
  const hasEditPermission = !!userAccount && userAccount.role !== 'normal'

  const [keywords, setKeywords] = useState('')
  const [searchKeywords, setSearchKeywords] = useState('')

  const hasFilterCondition = !!keywords
  const handleResetFilter = useCallback(() => {
    setKeywords('')
    setSearchKeywords('')
  }, [])

  const { run: handleSearch } = useDebounceFn(() => {
    setSearchKeywords(keywords)
  }, { wait: 500 })

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

  const {
    data,
    isLoading,
    isError,
    // extend: start sync app
    refetch,
    // extend: stop sync app
  } = useExploreAppList()

  // extend: start sync app — 推荐列表中的 app_id 集合，用于判断是否已同步
  const recommendedAppIds = useMemo(() => {
    if (!data)
      return new Set<string>()
    return new Set(data.allList.map(item => item.app_id))
  }, [data])
  // extend: stop sync app

  const filteredList = useMemo(() => {
    if (!data)
      return []
    let result = data.allList.filter(item => (
      currCategory === allCategoriesEn
      || item.categories?.includes(currCategory)
    ))
    // Extend: start Explore Add Search — 标签过滤（基于分类名）
    if (tagFilterValue.length > 0)
      result = result.filter(item => item.categories?.some(category => tagFilterValue.includes(category)))
    // Extend: stop Explore Add Search
    return result
  }, [data, currCategory, allCategoriesEn, tagFilterValue])

  const searchFilteredList = useMemo(() => {
    if (!searchKeywords || !filteredList || filteredList.length === 0)
      return filteredList

    const lowerCaseSearchKeywords = searchKeywords.toLowerCase()

    // Extend: 搜索同时匹配应用名与描述
    return filteredList.filter(item =>
      (item.app && item.app.name && item.app.name.toLowerCase().includes(lowerCaseSearchKeywords))
      || (typeof item.description === 'string' && item.description.toLowerCase().includes(lowerCaseSearchKeywords)),
    )
  }, [searchKeywords, filteredList])

  const [currApp, setCurrApp] = useState<App | null>(null)
  const [isShowCreateModal, setIsShowCreateModal] = useState(false)

  const {
    handleImportDSL,
    handleImportDSLConfirm,
    versions,
    isFetching,
  } = useImportDSL()
  const [showDSLConfirmModal, setShowDSLConfirmModal] = useState(false)

  const [currentTryApp, setCurrentTryApp] = useState<TryAppSelection | undefined>(undefined)
  const currentCreateAppModeRef = useRef<App['app']['mode'] | null>(null)
  const currentCreateAppTrackingRef = useRef<Pick<TrackCreateAppParams, 'source' | 'templateId'> | null>(null)
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
  const trackCurrentCreateApp = useCallback((appMode?: App['app']['mode'] | null) => {
    const currentCreateAppTracking = currentCreateAppTrackingRef.current
    const resolvedAppMode = appMode ?? currentCreateAppModeRef.current
    if (!resolvedAppMode || !currentCreateAppTracking)
      return

    trackCreateApp({
      ...currentCreateAppTracking,
      appMode: resolvedAppMode,
    })
    currentCreateAppTrackingRef.current = null
    currentCreateAppModeRef.current = null
  }, [])

  const onCreate: CreateAppModalProps['onConfirm'] = useCallback(async ({
    name,
    icon_type,
    icon,
    icon_background,
    description,
  }) => {
    hideTryAppPanel()

    const { export_data, mode } = await fetchAppDetail(
      currApp?.app.id as string,
    )
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
  }, [currApp?.app.id, handleImportDSL, hideTryAppPanel, trackCurrentCreateApp])

  const onConfirmDSL = useCallback(async () => {
    await handleImportDSLConfirm({
      onSuccess: (response) => {
        trackCurrentCreateApp(response.app_mode)
        onSuccess?.()
      },
    })
  }, [handleImportDSLConfirm, onSuccess, trackCurrentCreateApp])

  if (isLoading) {
    return (
      <div className="flex h-full items-center">
        <Loading type="area" />
      </div>
    )
  }

  if (isError || !data)
    return null

  const { categories } = data

  return (
    <div className={cn(
      'flex h-full min-h-0 flex-col overflow-hidden border-l-[0.5px] border-divider-regular',
    )}
    >
      <div className="flex flex-1 flex-col overflow-y-auto">
        {systemFeatures.enable_explore_banner && (
          <div className="mt-4 px-12">
            <Banner />
          </div>
        )}

        <div className="sticky top-0 z-10 bg-background-body">
          <div className={cn(
            'flex items-center justify-between px-12 pt-6',
          )}
          >
            <div className="flex items-center">
              <div className="grow truncate system-xl-semibold text-text-primary">{!hasFilterCondition ? t('apps.title', { ns: 'explore' }) : t('apps.resultNum', { num: searchFilteredList.length, ns: 'explore' })}</div>
              {hasFilterCondition && (
                <>
                  <div className="mx-3 h-4 w-px bg-divider-regular"></div>
                  <Button size="medium" onClick={handleResetFilter}>{t('apps.resetFilter', { ns: 'explore' })}</Button>
                </>
              )}
            </div>
            {/* Extend: start Explore Add Search */}
            <div className="flex items-center gap-2 self-start">
              <TagFilter type="app" value={tagFilterValue} onChange={handleTagsChange} />
              <Input
                showLeftIcon
                showClearIcon
                wrapperClassName="w-[200px]"
                value={keywords}
                onChange={e => handleKeywordsChange(e.target.value)}
                onClear={() => handleKeywordsChange('')}
              />
            </div>
            {/* Extend: stop Explore Add Search */}
          </div>

          <div className="px-12 pt-2 pb-4">
            <Category
              list={categories}
              value={currCategory}
              onChange={setCurrCategory}
              allCategoriesEn={allCategoriesEn}
            />
          </div>
        </div>

        <div className={cn(
          'relative flex flex-1 shrink-0 grow flex-col pb-6',
        )}
        >
          <nav
            className={cn(
              s.appList,
              'grid shrink-0 content-start gap-4 px-6 sm:px-12',
            )}
          >
            {searchFilteredList.map(app => (
              <AppCard
                key={app.app_id}
                app={app}
                canCreate={hasEditPermission}
                onCreate={() => {
                  currentCreateAppTrackingRef.current = {
                    source: 'explore_template_list',
                    templateId: app.app_id,
                  }
                  setCurrApp(app)
                  setIsShowCreateModal(true)
                }}
                onTry={handleTryApp}
                // extend: start sync app
                onApp={recommendedAppIds.has(app.app_id)}
                onRefresh={() => refetch()}
                // extend: stop sync app
              />
            ))}
          </nav>
        </div>
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
      {
        showDSLConfirmModal && (
          <DSLConfirmModal
            versions={versions}
            onCancel={() => setShowDSLConfirmModal(false)}
            onConfirm={onConfirmDSL}
            confirmDisabled={isFetching}
          />
        )
      }

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
