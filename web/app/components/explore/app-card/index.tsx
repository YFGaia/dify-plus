'use client'
import type { App } from '@/models/explore'
import type { TryAppSelection } from '@/types/try-app'
// extend: start sync app — AlertDialog/Button/toast 供「同步到应用模板」使用
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@langgenius/dify-ui/alert-dialog'
import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { toast } from '@langgenius/dify-ui/toast'
import { useCallback, useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { trackEvent } from '@/app/components/base/amplitude'
import AppIcon from '@/app/components/base/app-icon'
import { IS_CLOUD_EDITION } from '@/config'
import { useAppContext } from '@/context/app-context'
import { syncApp } from '@/service/apps'
// extend: stop sync app
import { AppModeEnum } from '@/types/app'
import { AppTypeIcon } from '../../app/type-selector'

export type AppCardProps = {
  app: App
  canCreate: boolean
  onCreate: () => void
  onTry: (params: TryAppSelection) => void
  // extend: start sync app
  onApp?: boolean // 是否在推荐列表中（已同步）
  onRefresh?: () => void
  // extend: stop sync app
  isExplore?: boolean
}

const AppCard = ({
  app,
  canCreate,
  onCreate,
  onTry,
  isExplore = true,
  onApp,
  onRefresh,
}: AppCardProps) => {
  const { t } = useTranslation()
  const { userProfile } = useAppContext() // extend: sync app 权限判断
  const nameId = useId()
  const descriptionId = useId()
  const { app: appBasicInfo } = app
  const canViewApp = IS_CLOUD_EDITION
  const isClickable = isExplore && (canViewApp || canCreate)
  const handleTryApp = () => {
    trackEvent('preview_template', {
      template_id: app.app_id,
      template_name: appBasicInfo.name,
      template_mode: appBasicInfo.mode,
      template_categories: app.categories,
      page: 'explore',
    })
    onTry({ appId: app.app_id, app })
  }
  const handleCardClick = () => {
    if (IS_CLOUD_EDITION) {
      handleTryApp()
      return
    }

    if (canCreate)
      onCreate()
  }

  // ----------------------start SyncToAppTemplate----------------------
  const [showSyncApps, setShowSyncApps] = useState(false)

  // app click sync
  const onSyncApps = useCallback(async () => {
    try {
      await syncApp({ appID: app.app_id })
      toast.success(t('app.syncAppOk', { ns: 'extend' }))
      if (onRefresh)
        onRefresh()
    }
    catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '操作失败')
    }
    setShowSyncApps(false)
  }, [app.app_id, onRefresh, t])
  // ----------------------stop SyncToAppTemplate----------------------

  return (
    <div
      className={cn(
        'group relative col-span-1 flex h-35.5 flex-col overflow-hidden rounded-xl border-[0.5px] border-components-panel-border bg-components-panel-on-panel-item-bg pb-3 text-left shadow-xs shadow-shadow-shadow-3',
        isClickable && 'cursor-pointer',
      )}
    >
      {isClickable && (
        <button
          type="button"
          className="absolute inset-0 z-10 cursor-pointer appearance-none rounded-xl border-0 bg-transparent p-0 outline-hidden focus-visible:ring-2 focus-visible:ring-state-accent-solid focus-visible:ring-inset"
          aria-labelledby={nameId}
          aria-describedby={app.description ? descriptionId : undefined}
          onClick={handleCardClick}
        />
      )}
      <div className="flex shrink-0 items-center gap-3 px-4 pt-4 pb-2">
        <div className="relative shrink-0">
          <AppIcon
            size="large"
            iconType={appBasicInfo.icon_type}
            icon={appBasicInfo.icon}
            background={appBasicInfo.icon_background}
            imageUrl={appBasicInfo.icon_url}
          />
          <AppTypeIcon
            wrapperClassName="absolute -right-0.5 -bottom-0.5 size-4 rounded-sm border-components-panel-on-panel-item-bg shadow-sm"
            className="size-3"
            type={appBasicInfo.mode}
          />
        </div>
        <div className="flex w-0 grow flex-col gap-1 py-px">
          <div className="flex items-center system-md-semibold text-text-secondary">
            <div id={nameId} className="truncate" title={appBasicInfo.name}>{appBasicInfo.name}</div>
          </div>
          <div className="flex items-center system-2xs-medium-uppercase text-text-tertiary">
            {appBasicInfo.mode === AppModeEnum.ADVANCED_CHAT && <div className="truncate">{t('types.advanced', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.CHAT && <div className="truncate">{t('types.chatbot', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.AGENT_CHAT && <div className="truncate">{t('types.agent', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.WORKFLOW && <div className="truncate">{t('types.workflow', { ns: 'app' }).toUpperCase()}</div>}
            {appBasicInfo.mode === AppModeEnum.COMPLETION && <div className="truncate">{t('types.completion', { ns: 'app' }).toUpperCase()}</div>}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-start px-4 py-1">
        <div id={descriptionId} className="line-clamp-2 min-h-8 flex-1 system-xs-regular text-text-tertiary">
          {app.description}
        </div>
      </div>
      {/* ----------------------start SyncToAppTemplate---------------------- */}
      {isExplore && userProfile?.admin_extend && userProfile?.tenant_extend && !onApp && (
        // z-20 保证按钮位于上游整卡点击遮罩（z-10）之上
        <div className={cn('absolute top-2 right-2 z-20 hidden items-center gap-1 group-hover:flex')}>
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
      <AlertDialog open={showSyncApps} onOpenChange={setShowSyncApps}>
        <AlertDialogContent>
          <div className="flex flex-col gap-2 px-6 pt-6 pb-4">
            <AlertDialogTitle className="title-2xl-semi-bold text-text-primary">
              {t('app.confirmSyncApp', { ns: 'extend' })}
            </AlertDialogTitle>
            <AlertDialogDescription className="w-full system-md-regular wrap-break-word whitespace-pre-wrap text-text-tertiary">
              {t('app.confirmSyncAppContent', { ns: 'extend' })}
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton>
              {t('operation.cancel', { ns: 'common' })}
            </AlertDialogCancelButton>
            <AlertDialogConfirmButton onClick={onSyncApps}>
              {t('operation.confirm', { ns: 'common' })}
            </AlertDialogConfirmButton>
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
      {/* ----------------------stop SyncToAppTemplate---------------------- */}
    </div>
  )
}

export default AppCard
