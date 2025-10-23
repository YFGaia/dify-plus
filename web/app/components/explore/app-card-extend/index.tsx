'use client'
import cn from 'classnames'
import { useTranslation } from 'react-i18next'
import { PlusIcon } from '@heroicons/react/20/solid'
import { useRouter, useSearchParams } from 'next/navigation'
import Button from '../../base/button'
import type { App } from '@/models/explore'
import AppIcon from '@/app/components/base/app-icon'
import { AiText, ChatBot, CuteRobot } from '@/app/components/base/icons/src/vender/solid/communication'
import { Route } from '@/app/components/base/icons/src/vender/solid/mapsAndTravel'
export type AppCardProps = {
  app: App
  canCreate: boolean
  isExplore: boolean
}

const AppCard = ({
  app,
  canCreate,
  isExplore,
}: AppCardProps) => {
  const { t } = useTranslation()
  const { app: appBasicInfo } = app
  const router = useRouter()

  // ------------------------ start You must log in to access your account extend ------------------------
  const searchParams = useSearchParams()
  const consoleTokenFromLocalStorage = localStorage?.getItem('console_token')
  const consoleToken = searchParams.get('access_token') ? searchParams.get('access_token') : searchParams.get('console_token')

  if (!(consoleToken || consoleTokenFromLocalStorage)) {
    if (window.location !== undefined)
      localStorage?.setItem('redirect_url', window.location.href)
    router.replace('/signin')
    return null
  }
  // ------------------------ end You must log in to access your account extend ------------------------
  const openChat = () => {
    router.replace(`/explore/installed/${app.app.id}`)
  }

  return (
    <div className={cn('group relative col-span-1 flex cursor-pointer flex-col overflow-hidden rounded-lg border-[0.5px] border-components-panel-border bg-components-panel-on-panel-item-bg pb-2 shadow-sm transition-all duration-200 ease-in-out hover:shadow-lg')}>
      <div className='flex h-[66px] shrink-0 grow-0 items-center gap-3 px-[14px] pb-3 pt-[14px]'>
        <div className='relative shrink-0'>
          <AppIcon
            size='large'
            iconType={app.app.icon_type}
            icon={app.app.icon_url ? app.app.icon_url : app.app.icon}
            background={app.app.icon_background}
            imageUrl={app.app.icon_url}
          />
          <span className='absolute bottom-[-3px] right-[-3px] w-4 h-4 p-0.5 rounded border-[0.5px] border-[rgba(0,0,0,0.02)] shadow-sm'>
            {appBasicInfo.mode === 'advanced-chat' && (
              <ChatBot className='w-3 h-3 text-[#1570EF]' />
            )}
            {appBasicInfo.mode === 'agent-chat' && (
              <CuteRobot className='w-3 h-3 text-indigo-600' />
            )}
            {appBasicInfo.mode === 'chat' && (
              <ChatBot className='w-3 h-3 text-[#1570EF]' />
            )}
            {appBasicInfo.mode === 'completion' && (
              <AiText className='w-3 h-3 text-[#0E9384]' />
            )}
            {appBasicInfo.mode === 'workflow' && (
              <Route className='w-3 h-3 text-[#f79009]' />
            )}
          </span>
        </div>
        <div className='grow w-0 py-[1px]'>
          <div className='flex items-center text-sm font-semibold leading-5 text-text-secondary'>
            <div className='truncate' title={appBasicInfo.name}>{appBasicInfo.name}</div>
          </div>
          <div className='flex items-center text-[10px] leading-[18px] text-gray-500 font-medium'>
            {appBasicInfo.mode === 'advanced-chat' && <div className='truncate'>{t('app.types.chatbot').toUpperCase()}</div>}
            {appBasicInfo.mode === 'chat' && <div className='truncate'>{t('app.types.chatbot').toUpperCase()}</div>}
            {appBasicInfo.mode === 'agent-chat' && <div className='truncate'>{t('app.types.agent').toUpperCase()}</div>}
            {appBasicInfo.mode === 'workflow' && <div className='truncate'>{t('app.types.workflow').toUpperCase()}</div>}
            {appBasicInfo.mode === 'completion' && <div className='truncate'>{t('app.types.completion').toUpperCase()}</div>}
          </div>
        </div>
      </div>
      <div className='mb-1 px-[14px] text-xs leading-normal text-gray-500 line-clamp-4 group-hover:line-clamp-2 group-hover:h-9'>{app.description}</div>

      <div className={cn('hidden items-center flex-wrap min-h-[42px] px-[14px] pt-2 pb-[10px] group-hover:flex')}>
        <div className={cn('flex items-center w-full space-x-2')}>
          <Button variant='primary' className='grow h-7' onClick={() => openChat()}>
            <PlusIcon className='w-4 h-4 mr-1' />
            <span className='text-xs'>{t('share.chat.newChatDefaultName')}</span>
          </Button>
        </div>
      </div>
    </div>
  )
}

export default AppCard
