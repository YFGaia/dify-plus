'use client'

// 二开部分：系统管理入口。原实现位于旧 web/app/components/header/system-manage-nav-extend/
// （上游 1.15.0 删除旧 header 后重做到 main-nav 体系，复用 MainNavLink 样式约定）。
// Instance Casdoor management is separate from workspace ownership.
import type { MainNavItem } from '../types'
import { useAtomValue } from 'jotai'
import { useTranslation } from 'react-i18next'
// extend: 上游 1.16.0 删除 app-context，owner 判定改用 workspace-state 原子
import { isCurrentWorkspaceOwnerAtom } from '@/context/workspace-state'
import { useCasdoorManagementAccess } from '@/features/casdoor/management-access/use-casdoor-management-access'
import MainNavLink from './nav-link'

const isSystemManagePath = (path: string) =>
  path === '/system-manage-extend' || path.startsWith('/system-manage-extend/')

type SystemManageNavExtendProps = {
  pathname: string
}

const SystemManageNavExtend = ({ pathname }: SystemManageNavExtendProps) => {
  const { t } = useTranslation()
  const isCurrentWorkspaceOwner = useAtomValue(isCurrentWorkspaceOwnerAtom)
  const { canManageCasdoor } = useCasdoorManagementAccess()

  if (!isCurrentWorkspaceOwner && !canManageCasdoor) return null

  const item: MainNavItem = {
    href: isCurrentWorkspaceOwner
      ? '/system-manage-extend/system-integration'
      : '/system-manage-extend/system-integration?tab=casdoor',
    label: t(($) => $['systemManage.title'], { ns: 'extend' }),
    active: isSystemManagePath,
    icon: 'i-ri-settings-3-line',
    activeIcon: 'i-ri-settings-3-fill',
  }

  return <MainNavLink item={item} pathname={pathname} />
}

export default SystemManageNavExtend
