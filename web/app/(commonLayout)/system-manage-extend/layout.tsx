'use client'

import type { ReactNode } from 'react'
import { cn } from '@langgenius/dify-ui/cn'
import { useAtomValue } from 'jotai'
import { useTranslation } from 'react-i18next'
// extend: 上游 1.16.0 删除 app-context，owner 判定改用 workspace-state 原子
import { isCurrentWorkspaceOwnerAtom } from '@/context/workspace-state'
import { useCasdoorManagementAccess } from '@/features/casdoor/management-access/use-casdoor-management-access'
import Link from '@/next/link'
import { usePathname, useSelectedLayoutSegment } from '@/next/navigation'

type MenuItemType = {
  key: string
  label: string
  href: string
}

const SystemManageLayout = ({ children }: { children: ReactNode }) => {
  const { t } = useTranslation()
  const isCurrentWorkspaceOwner = useAtomValue(isCurrentWorkspaceOwnerAtom)
  const selectedSegment = useSelectedLayoutSegment()
  const pathname = usePathname()
  const { canManageCasdoor, isPending } = useCasdoorManagementAccess()
  const isIntegrationRoute = pathname === '/system-manage-extend/system-integration'

  // Instance permission admits only this exact page, never a management subtree.
  if (!isCurrentWorkspaceOwner && (!canManageCasdoor || !isIntegrationRoute)) {
    return (
      <div className="flex h-full items-center justify-center">
        <div
          role={isPending && isIntegrationRoute ? 'status' : 'alert'}
          className="text-center text-text-tertiary"
        >
          {t(
            ($) =>
              $[
                isPending && isIntegrationRoute
                  ? 'systemManage.casdoor.loading'
                  : 'systemManage.common.noPermission'
              ],
            { ns: 'extend' },
          )}
        </div>
      </div>
    )
  }

  const menuItems: MenuItemType[] = [
    {
      key: 'system-integration',
      label: t(($) => $['systemManage.menu.integration'], { ns: 'extend' }),
      href: isCurrentWorkspaceOwner
        ? '/system-manage-extend/system-integration'
        : '/system-manage-extend/system-integration?tab=casdoor',
    },
    {
      key: 'quota-management',
      label: t(($) => $['systemManage.menu.quota'], { ns: 'extend' }),
      href: '/system-manage-extend/quota-management',
    },
    {
      key: 'code-execution-control',
      label: t(($) => $['systemManage.menu.codeExecutionControl'], { ns: 'extend' }),
      href: '/system-manage-extend/code-execution-control',
    },
  ]

  return (
    <div className="flex h-full overflow-hidden">
      {/* 左侧菜单 */}
      <div className="flex w-[220px] shrink-0 flex-col border-r border-divider-subtle bg-background-default-subtle px-3 py-4">
        <h2 className="mb-4 px-3 text-base font-semibold text-text-primary">
          {t(($) => $['systemManage.title'], { ns: 'extend' })}
        </h2>
        <nav
          aria-label={t(($) => $['systemManage.title'], { ns: 'extend' })}
          className="flex flex-col gap-0.5"
        >
          {menuItems
            .filter((item) => isCurrentWorkspaceOwner || item.key === 'system-integration')
            .map((item) => (
              <Link
                key={item.key}
                href={item.href}
                aria-current={selectedSegment === item.key ? 'page' : undefined}
                className={cn(
                  'rounded-lg px-3 py-2 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-state-accent-solid',
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
      <div className="flex-1 overflow-y-auto bg-background-body p-6">{children}</div>
    </div>
  )
}

export default SystemManageLayout
