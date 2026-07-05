'use client'

import { cn } from '@langgenius/dify-ui/cn'
import { RiSettings3Fill, RiSettings3Line } from '@remixicon/react'
import { useSelectedLayoutSegment } from 'next/navigation'
import { useTranslation } from 'react-i18next'
import { useAppContext } from '@/context/app-context'
import Link from '@/next/link'

type Props = {
  className?: string
}

const SystemManageNavExtend = ({ className }: Props) => {
  const { t } = useTranslation()
  const { isCurrentWorkspaceOwner } = useAppContext()
  const selectedSegment = useSelectedLayoutSegment()
  const activated = selectedSegment === 'system-manage-extend'

  // 仅 owner 可见
  if (!isCurrentWorkspaceOwner)
    return null

  return (
    <Link
      href="/system-manage-extend/system-integration"
      className={cn(
        className,
        'group',
        activated && 'bg-components-main-nav-nav-button-bg-active shadow-md',
        activated
          ? 'text-components-main-nav-nav-button-text-active'
          : 'text-components-main-nav-nav-button-text hover:bg-components-main-nav-nav-button-bg-hover',
      )}
    >
      {activated ? <RiSettings3Fill className="size-4" /> : <RiSettings3Line className="size-4" />}
      <div className="ml-2 max-[1024px]:hidden">
        {t('systemManage.title', { ns: 'extend' })}
      </div>
    </Link>
  )
}

export default SystemManageNavExtend
