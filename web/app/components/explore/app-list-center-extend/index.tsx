'use client'

import type { InstalledApp } from '@/service/explore'
import { cn } from '@langgenius/dify-ui/cn'
import { useQuery } from '@tanstack/react-query'
import { useQueryState } from 'nuqs'
import * as React from 'react'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Loading from '@/app/components/base/loading'
// Extend: start Explore Add Search
import { SearchInput } from '@/app/components/base/search-input'
import AppCard from '@/app/components/explore/app-card-extend'
import Category from '@/app/components/explore/category'
import { buildInstalledAppPath } from '@/app/components/explore/installed-app/routes'
import { TagFilter } from '@/features/tag-management/components/tag-filter'
import { useRouter } from '@/next/navigation'
import { consoleQuery } from '@/service/console'
import { useInstalledAppList } from '@/service/use-explore'
import s from './style.module.css'
// Extend: stop Explore Add Search

const Apps = () => {
  const { t } = useTranslation()
  const allCategoriesEn = t(($) => $['apps.allCategories'], { ns: 'explore', lng: 'en' })

  // Extend: start Installed app list sorted by usage
  const { data, isLoading, isError } = useInstalledAppList()
  // Extend: stop Installed app list sorted by usage

  // Extend: start Explore Add Search
  const [tagFilterValue, setTagFilterValue] = useState<string[]>([])
  const [keywordsValue, setKeywordsValue] = useState<string>('')
  // Extend: stop Explore Add Search

  const { data: tags = [], isError: isTagsError } = useQuery(
    consoleQuery.tags.get.queryOptions({ input: { query: { type: 'app' } } }),
  )

  const [currCategory, setCurrCategory] = useQueryState('category', {
    defaultValue: allCategoriesEn,
  })

  // Extend: start Filtered list with search and tag filter
  const filteredListExtend = useMemo(() => {
    if (!data) return []

    let result = data.allList

    // Apply category filter（后端 /installed/apps 每个 tag 输出一行，字段为单数 category）
    if (currCategory !== allCategoriesEn) {
      result = result.filter((item) => item.category === currCategory)
    }

    // Apply tag filter
    if (tagFilterValue.length > 0) {
      const selectedNames = new Set(
        tags.filter((tag) => tagFilterValue.includes(tag.id)).map((tag) => tag.name),
      )
      result = result.filter((item) => selectedNames.has(item.category))
    }

    // Apply keyword search
    if (keywordsValue.length > 0) {
      const lowerCaseKeywords = keywordsValue.toLowerCase()
      result = result.filter(
        (item) =>
          item.description?.toLowerCase().includes(lowerCaseKeywords) ||
          item.app?.name?.toLowerCase().includes(lowerCaseKeywords),
      )
    }

    // Deduplicate by app_id (same app may appear multiple times due to multiple tags)
    const seenAppIds = new Set<string>()
    const deduplicatedResult: InstalledApp[] = []
    for (const item of result) {
      if (!seenAppIds.has(item.app_id)) {
        seenAppIds.add(item.app_id)
        deduplicatedResult.push(item)
      }
    }

    return deduplicatedResult
  }, [data, currCategory, allCategoriesEn, tagFilterValue, keywordsValue, tags])
  // Extend: stop Filtered list with search and tag filter

  // Extend: start Create new conversation for installed app
  const { push } = useRouter()
  const handleCreateConversation = useCallback(
    (app: InstalledApp) => {
      push(buildInstalledAppPath(app.installed_id))
    },
    [push],
  )
  // Extend: stop Create new conversation for installed app

  if (isLoading) {
    return (
      <div className="flex h-full items-center">
        <Loading type="area" />
      </div>
    )
  }

  if (isError || isTagsError || !data) {
    return (
      <div role="alert" className="flex h-full items-center justify-center text-text-secondary">
        {t(($) => $['errorBoundary.title'], { ns: 'common' })}
      </div>
    )
  }

  const { categories } = data

  return (
    <div className={cn('flex h-full flex-col border-l-[0.5px] border-divider-regular')}>
      <div className={cn('mt-6 flex items-center justify-between px-12')}>
        <Category
          list={categories}
          value={currCategory}
          onChange={setCurrCategory}
          allCategoriesEn={allCategoriesEn}
        />
        {/* Extend: start Explore Add Search */}
        <div className="flex items-center gap-2">
          <TagFilter type="app" value={tagFilterValue} onChange={setTagFilterValue} />
          <SearchInput
            className="w-[200px]"
            value={keywordsValue}
            onValueChange={setKeywordsValue}
          />
        </div>
        {/* Extend: stop Explore Add Search */}
      </div>
      <div className={cn('relative mt-4 flex flex-1 shrink-0 grow flex-col overflow-auto pb-6')}>
        <nav className={cn(s.appList, 'grid shrink-0 content-start gap-4 px-6 sm:px-12')}>
          {filteredListExtend.map((app) => (
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
