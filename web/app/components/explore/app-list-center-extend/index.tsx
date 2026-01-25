'use client'

import type { App } from '@/models/explore'
import { useDebounceFn } from 'ahooks'
import { useQueryState } from 'nuqs'
import { useRouter } from 'next/navigation'
import * as React from 'react'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Loading from '@/app/components/base/loading'
import Category from '@/app/components/explore/category'
import { useInstalledAppList } from '@/service/use-explore'
import { cn } from '@/utils/classnames'
import s from './style.module.css'
// Extend: start Explore Add Search
import SearchInput from '@/app/components/base/search-input'
import TagFilter from '@/app/components/base/tag-management/filter'
import AppCard from '@/app/components/explore/app-card-extend'
// Extend: stop Explore Add Search

type AppsProps = {
  onSuccess?: () => void
}

const Apps = ({
  onSuccess,
}: AppsProps) => {
  const { t } = useTranslation()
  const allCategoriesEn = t('apps.allCategories', { ns: 'explore', lng: 'en' })

  // Extend: start Installed app list sorted by usage
  const {
    data,
    isLoading,
    isError,
  } = useInstalledAppList()
  // Extend: stop Installed app list sorted by usage

  // Extend: start Explore Add Search
  const [tagFilterValue, setTagFilterValue] = useState<string[]>([])
  const [keywordsValue, setKeywordsValue] = useState<string>('')
  // Extend: stop Explore Add Search

  const { run: handleSearch } = useDebounceFn(() => {
    // Trigger search update
  }, { wait: 500 })

  const handleTagsChange = (value: string[]) => {
    setTagFilterValue(value)
    handleSearch()
  }

  const handleKeywordsChange = (value: string) => {
    setKeywordsValue(value)
    handleSearch()
  }

  const [currCategory, setCurrCategory] = useQueryState('category', {
    defaultValue: allCategoriesEn,
  })

  // Extend: start Filtered list with search and tag filter
  const filteredListExtend = useMemo(() => {
    if (!data)
      return []

    let result = data.allList

    // Apply category filter
    if (currCategory !== allCategoriesEn) {
      result = result.filter(item => item.category === currCategory)
    }

    // Apply tag filter
    if (tagFilterValue.length > 0) {
      result = result.filter(item => tagFilterValue.includes(item.category))
    }

    // Apply keyword search
    if (keywordsValue.length > 0) {
      const lowerCaseKeywords = keywordsValue.toLowerCase()
      result = result.filter(item =>
        item.description?.toLowerCase().includes(lowerCaseKeywords) ||
        item.app?.name?.toLowerCase().includes(lowerCaseKeywords)
      )
    }

    // Deduplicate by app_id (same app may appear multiple times due to multiple tags)
    const seenAppIds = new Set<string>()
    const deduplicatedResult: App[] = []
    for (const item of result) {
      if (!seenAppIds.has(item.app_id)) {
        seenAppIds.add(item.app_id)
        deduplicatedResult.push(item)
      }
    }

    return deduplicatedResult
  }, [data, currCategory, allCategoriesEn, tagFilterValue, keywordsValue])
  // Extend: stop Filtered list with search and tag filter

  // Extend: start Create new conversation for installed app
  const { push } = useRouter()
  const handleCreateConversation = useCallback((app: App) => {
    // Directly navigate to installed app conversation page
    // Use installed_id which should be provided by backend
    if (app.installed_id) {
      push(`/explore/installed/${app.installed_id}`)
    }
  }, [push])
  // Extend: stop Create new conversation for installed app

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
      'flex h-full flex-col border-l-[0.5px] border-divider-regular',
    )}
    >
      <div className={cn(
        'mt-6 flex items-center justify-between px-12',
      )}
      >
        <Category
          list={categories}
          value={currCategory}
          onChange={setCurrCategory}
          allCategoriesEn={allCategoriesEn}
        />
        {/* Extend: start Explore Add Search */}
        <div className="flex items-center gap-2">
          <TagFilter type="app" value={tagFilterValue} onChange={handleTagsChange} />
          <SearchInput className="w-[200px]" value={keywordsValue} onChange={handleKeywordsChange}/>
        </div>
        {/* Extend: stop Explore Add Search */}
      </div>
      <div className={cn(
        'relative mt-4 flex flex-1 shrink-0 grow flex-col overflow-auto pb-6',
      )}
      >
        <nav
          className={cn(
            s.appList,
            'grid shrink-0 content-start gap-4 px-6 sm:px-12',
          )}
        >
          {filteredListExtend.map(app => (
            <AppCard
              key={app.installed_id}
              isExplore
              app={app}
              // Extend: start Create new conversation for installed app
              onCreate={() => handleCreateConversation(app)}
              canCreate={true}
              // Extend: stop Create new conversation for installed app
            />
          ))}
        </nav>
      </div>
    </div>
  )
}

export default React.memo(Apps)
