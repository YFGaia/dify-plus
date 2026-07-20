'use client'

// 二开部分：系统管理入口。原实现位于旧 web/app/components/header/system-manage-nav-extend/
// （上游 1.15.0 删除旧 header 后重做到 main-nav 体系，复用 MainNavLink 样式约定）。
// 可见性口径与迁移前一致：仅 workspace owner 可见。
import type { MainNavItem } from '../types'
import { useAtomValue } from 'jotai'
import { useTranslation } from 'react-i18next'
// extend: 上游 1.16.0 删除 app-context，owner 判定改用 workspace-state 原子
import { isCurrentWorkspaceOwnerAtom } from '@/context/workspace-state'
import MainNavLink from './nav-link'

const isSystemManagePath = (path: string) =>
  path === '/system-manage-extend' || path.startsWith('/system-manage-extend/')

type SystemManageNavExtendProps = {
  pathname: string
}

const SystemManageNavExtend = ({ pathname }: SystemManageNavExtendProps) => {
  const { t } = useTranslation()
  const isCurrentWorkspaceOwner = useAtomValue(isCurrentWorkspaceOwnerAtom)

  if (!isCurrentWorkspaceOwner)
    return null

  const item: MainNavItem = {
    href: '/system-manage-extend/system-integration',
    label: t('systemManage.title', { ns: 'extend' }),
    active: isSystemManagePath,
    icon: 'i-ri-settings-3-line',
    activeIcon: 'i-ri-settings-3-fill',
  }

  return <MainNavLink item={item} pathname={pathname} />
}

export default SystemManageNavExtend
