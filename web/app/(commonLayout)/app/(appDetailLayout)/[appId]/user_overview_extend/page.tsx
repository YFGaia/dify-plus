'use client'
import type { PeriodParams } from '@/app/components/app/overview/app-chart'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { useSuspenseQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import quarterOfYear from 'dayjs/plugin/quarterOfYear'
import { useAtomValue } from 'jotai'
import React, { use, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { TIME_PERIOD_MAPPING } from '@/app/components/app/log/filter'
import {
  AvgSessionInteractions,
  AvgUserInteractions,
  ConversationsChart,
  CostChart,
  WorkflowCostChart,
  WorkflowMessagesChart,
} from '@/app/components/app/overview/app-chart'
import { useStore as useAppStore } from '@/app/components/app/store'
import {
  workspacePermissionKeysAtom,
  workspacePermissionKeysLoadingAtom,
} from '@/context/permission-state'
import { currentWorkspaceAtom, currentWorkspaceLoadingAtom } from '@/context/workspace-state'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { getAppACLCapabilities } from '@/utils/permission'

dayjs.extend(quarterOfYear)

const today = dayjs()

const queryDateFormat = 'YYYY-MM-DD HH:mm'

export type UserOverViewProps = {
  params: Promise<{ appId: string }>
}

const UserOverView = ({ params }: UserOverViewProps) => {
  const { appId } = use(params)
  const { t } = useTranslation()
  const appDetail = useAppStore((state) => state.appDetail)
  const { data: currentUserId } = useSuspenseQuery({
    ...userProfileQueryOptions(),
    select: (data) => data.profile.id,
  })
  const workspacePermissionKeys = useAtomValue(workspacePermissionKeysAtom)
  const isLoadingWorkspacePermissionKeys = useAtomValue(workspacePermissionKeysLoadingAtom)
  const currentWorkspace = useAtomValue(currentWorkspaceAtom)
  const isLoadingCurrentWorkspace = useAtomValue(currentWorkspaceLoadingAtom)
  const { canMonitor } = getAppACLCapabilities(appDetail?.permission_keys, {
    currentUserId,
    resourceMaintainer: appDetail?.maintainer,
    workspacePermissionKeys,
  })
  const model = appDetail?.mode
  const isChatApp = model !== 'completion' && model !== 'workflow'
  const [period, setPeriod] = useState<PeriodParams>(() => ({
    name: t(($) => $['filter.period.last7days'], { ns: 'appLog' }),
    query: {
      start: today.subtract(7, 'day').startOf('day').format(queryDateFormat),
      end: today.format(queryDateFormat),
      account: true,
    },
  }))

  const onSelect = (item: { value: number; name: string }) => {
    if (item.value === -1) {
      // allTime
      setPeriod({ name: item.name, query: { account: true } })
    } else if (item.value === 0) {
      const startOfToday = today.startOf('day').format(queryDateFormat)
      const endOfToday = today.endOf('day').format(queryDateFormat)
      setPeriod({ name: item.name, query: { start: startOfToday, end: endOfToday, account: true } })
    } else {
      setPeriod({
        name: item.name,
        query: {
          start: today.subtract(item.value, 'day').startOf('day').format(queryDateFormat),
          end: today.format(queryDateFormat),
          account: true,
        },
      })
    }
  }

  if (
    appDetail?.id !== appId ||
    !currentWorkspace.id ||
    isLoadingCurrentWorkspace ||
    isLoadingWorkspacePermissionKeys ||
    !canMonitor
  )
    return null

  return (
    <div>
      <div className="mt-8 mb-4 flex flex-row items-center text-base text-gray-900">
        <span className="mr-3">{t(($) => $['appMenus.overview'], { ns: 'common' })}</span>
        <Select
          defaultValue="2"
          onValueChange={(k) => {
            const entry = TIME_PERIOD_MAPPING[k as keyof typeof TIME_PERIOD_MAPPING]
            if (entry)
              onSelect({
                value: entry.value,
                name: t(($) => $[`filter.period.${entry.name}`], { ns: 'appLog' }),
              })
          }}
        >
          <SelectTrigger className="mt-0 w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(TIME_PERIOD_MAPPING).map(([k, v]) => (
              <SelectItem key={k} value={k}>
                <SelectItemText>
                  {t(($) => $[`filter.period.${v.name}`], { ns: 'appLog' })}
                </SelectItemText>
                <SelectItemIndicator />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {model === 'workflow' && (
        <>
          {/* Extend: Workflow personal detection error */}
          <div className="mb-6 grid w-full grid-cols-1 gap-6 xl:grid-cols-2">
            <WorkflowMessagesChart period={period} id={appId} />
            <WorkflowCostChart period={period} id={appId} />
          </div>
          <div className="mb-6 grid w-full grid-cols-1 gap-6 xl:grid-cols-2">
            <AvgUserInteractions period={period} id={appId} />
          </div>
        </>
      )}
      {model !== 'workflow' && (
        <>
          <div className="mb-6 grid w-full grid-cols-1 gap-6 xl:grid-cols-2">
            <ConversationsChart period={period} id={appId} />
            {model !== 'completion' &&
              (isChatApp ? (
                <AvgSessionInteractions period={period} id={appId} />
              ) : (
                <AvgUserInteractions period={period} id={appId} />
              ))}
          </div>
          <div className="mb-6 grid w-full grid-cols-1 gap-6 xl:grid-cols-2">
            <CostChart period={period} id={appId} />
          </div>
        </>
      )}
    </div>
  )
}

export default UserOverView
