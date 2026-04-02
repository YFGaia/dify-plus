'use client'

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import Link from 'next/link'
import { useSelectedLayoutSegment } from 'next/navigation'
import { cn } from '@/utils/classnames'
import { useAppContext } from '@/context/app-context'

type MenuItemType = {
  key: string
  label: string
  href: string
}

const SystemManageLayout = ({ children }: { children: ReactNode }) => {
  const { t } = useTranslation()
  const { isCurrentWorkspaceOwner } = useAppContext()
  const selectedSegment = useSelectedLayoutSegment()

  // 非 owner 不可访问
  if (!isCurrentWorkspaceOwner) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center text-text-tertiary">
          {t('systemManage.common.noPermission', { ns: 'extend' })}
        </div>
      </div>
    )
  }

  const menuItems: MenuItemType[] = [
    {
      key: 'system-integration',
      label: t('systemManage.integration', { ns: 'extend' }),
      href: '/system-manage-extend/system-integration',
    },
  ]

  return (
    <div className="flex h-full overflow-hidden">
      {/* 左侧菜单 */}
      <div className="flex w-[220px] shrink-0 flex-col border-r border-divider-subtle bg-background-default-subtle px-3 py-4">
        <h2 className="mb-4 px-3 text-base font-semibold text-text-primary">
          {t('systemManage.title', { ns: 'extend' })}
        </h2>
        <nav className="flex flex-col gap-0.5">
          {menuItems.map(item => (
            <Link
              key={item.key}
              href={item.href}
              className={cn(
                'rounded-lg px-3 py-2 text-sm transition-colors',
                selectedSegment === item.key
                  ? 'bg-state-accent-active font-medium text-text-accent'
                  : 'text-text-secondary hover:bg-state-base-hover',
              )}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </div>

      {/* 右侧内容区 */}
      <div className="flex-1 overflow-y-auto bg-background-body p-6">
        {children}
      </div>
    </div>
  )
}

export default SystemManageLayout
