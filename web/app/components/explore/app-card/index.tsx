'use client'
import type { App } from '@/models/explore'
import { PlusIcon } from '@heroicons/react/20/solid'
import { RiInformation2Line } from '@remixicon/react'
import { useTranslation } from 'react-i18next'
// extend: start sync app
import { useContextSelector } from 'use-context-selector'
import { useCallback, useState } from 'react'
// extend: atop sync app
import { useContext } from 'use-context-selector'
import AppIcon from '@/app/components/base/app-icon'
import ExploreContext from '@/context/explore-context'
import { useGlobalPublicStore } from '@/context/global-public-context'
import { AppModeEnum } from '@/types/app'
import { cn } from '@/utils/classnames'
import { AppTypeIcon } from '../../app/type-selector'
import Button from '../../base/button'
import Confirm from '@/app/components/base/confirm'
import Toast, { ToastContext } from '@/app/components/base/toast'
// extend: start sync app
import { syncApp } from '@/service/apps'
// extend: stop sync app
import { useAppContext } from '@/context/app-context'

export type AppCardProps = {
  app: App
  canCreate: boolean
  onCreate: () => void
  isExplore: boolean
  // extend: start sync app
  onApp?: boolean // 是否在推荐列表中（已同步）
  onRefresh?: () => void
  // extend: stop sync app
}

const AppCard = ({
  app,
  canCreate,
  onCreate,
  isExplore,
  onApp = false,
  onRefresh,
}: AppCardProps) => {
  const { t } = useTranslation()
  const { notify } = useContext(ToastContext)
  const { userProfile } = useAppContext()
  const { app: appBasicInfo } = app
  const { systemFeatures } = useGlobalPublicStore()
  const isTrialApp = app.can_trial && systemFeatures.enable_trial_app
  const setShowTryAppPanel = useContextSelector(ExploreContext, ctx => ctx.setShowTryAppPanel)
  const showTryAPPPanel = useCallback((appId: string) => {
    return () => {
      setShowTryAppPanel?.(true, { appId, app })
    }
  }, [setShowTryAppPanel, app])


  // ----------------------start SyncToAppTemplate----------------------
  const [showSyncApps, setShowSyncApps] = useState(false)

  // app click sync
  const onSyncApps = useCallback(async () => {
    try {
      await syncApp({ appID: app.app_id })
      notify({ type: 'success', message: t('app.syncAppOk', { ns: 'extend' }) })
      if (onRefresh)
        onRefresh()
    }
    catch (e: any) {
      notify({
        type: 'error',
        message: e.message || '操作失败',
      })
    }
    setShowSyncApps(false)
  }, [app.app_id, notify, onRefresh, t])
  // ----------------------stop SyncToAppTemplate----------------------
  return (
    <div className={cn('group relative col-span-1 flex cursor-pointer flex-col overflow-hidden rounded-lg border-[0.5px] border-components-panel-border bg-components-panel-on-panel-item-bg pb-2 shadow-sm transition-all duration-200 ease-in-out hover:bg-components-panel-on-panel-item-bg-hover hover:shadow-lg')}>
      <div className="flex h-[66px] shrink-0 grow-0 items-center gap-3 px-[14px] pb-3 pt-[14px]">
        <div className="relative shrink-0">
          <AppIcon
            size="large"
            iconType={appBasicInfo.icon_type}
            icon={appBasicInfo.icon}
            background={appBasicInfo.icon_background}
            imageUrl={appBasicInfo.icon_url}
          />
          <AppTypeIcon
            wrapperClassName="absolute -bottom-0.5 -right-0.5 w-4 h-4 shadow-sm"
            className="h-3 w-3"
            type={appBasicInfo.mode}
          />
        </div>
        <div className="w-0 grow py-[1px]">
          <div className="flex items-center text-sm font-semibold leading-5 text-text-secondary">
            <div className="truncate" title={appBasicInfo.name}>{appBasicInfo.name}</div>
          </div>
          <div className="flex items-center text-[10px] font-medium leading-[18px] text-text-tertiary">
            {appBasicInfo.mode === AppModeEnum.ADVANCED_CHAT && <div className="truncate">{t('types.advanced', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.CHAT && <div className="truncate">{t('types.chatbot', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.AGENT_CHAT && <div className="truncate">{t('types.agent', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.WORKFLOW && <div className="truncate">{t('types.workflow', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.COMPLETION && <div className="truncate">{t('types.completion', { ns: 'app' }).toUpperCase()}</div>}
          </div>
        </div>
      </div>
      <div className="description-wrapper system-xs-regular h-[90px] px-[14px] text-text-tertiary">
        <div className="line-clamp-4 group-hover:line-clamp-2">
          {app.description}
        </div>
      </div>
      {isExplore && (canCreate || isTrialApp) && (
        <div className={cn('absolute bottom-0 left-0 right-0 hidden bg-gradient-to-t from-components-panel-gradient-2 from-[60.27%] to-transparent p-4 pt-8 group-hover:flex')}>
          <div className={cn('grid h-8 w-full grid-cols-1 space-x-2', isTrialApp && 'grid-cols-2')}>
            <Button variant="primary" className="h-7" onClick={() => onCreate()}>
              <PlusIcon className="mr-1 h-4 w-4" />
              <span className="text-xs">{t('appCard.addToWorkspace', { ns: 'explore' })}</span>
            </Button>
            {isTrialApp && (
              <Button className="h-7" onClick={showTryAPPPanel(app.app_id)}>
                <RiInformation2Line className="mr-1 size-4" />
                <span>{t('appCard.try', { ns: 'explore' })}</span>
              </Button>
            )}
          </div>
        </div>
      )}
      {/* ----------------------start SyncToAppTemplate---------------------- */}
      {isExplore && userProfile?.admin_extend && userProfile?.tenant_extend && !onApp && (
        <div className={cn('absolute top-2 right-2 hidden group-hover:flex items-center gap-1')}>
          <Button
            variant="ghost"
            size="small"
            className="h-7 px-2 text-xs"
            onClick={(e) => {
              e.stopPropagation()
              setShowSyncApps(true)
            }}
          >
            <span style={{ color: '#00931e' }}>{t('app.syncToAppTemplate', { ns: 'extend' })}</span>
          </Button>
        </div>
      )}
      {showSyncApps && (
        <Confirm
          title={t('app.confirmSyncApp', { ns: 'extend' })}
          content={t('app.confirmSyncAppContent', { ns: 'extend' })}
          isShow={showSyncApps}
          onConfirm={onSyncApps}
          onCancel={() => setShowSyncApps(false)}
        />
      )}
      {/* ----------------------stop SyncToAppTemplate---------------------- */}
    </div>
  )
}

export default AppCard
