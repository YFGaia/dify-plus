'use client'

// extend: 二开权限位状态。
// 上游 1.16.0 删除 app-context（use-context-selector 的 AppContextProvider），
// fork 原挂在 userProfile 上的 admin_extend/tenant_extend（超管/超管租户判定）
// 迁移到 jotai 原子体系：数据仍来自 current workspace 响应
// （见 app-context-normalizers.ts 的 normalizeCurrentWorkspace extend 块）。
import { atom, useAtomValue } from 'jotai'
import { currentWorkspaceAtom } from './workspace-state'

export const adminExtendAtom = atom((get) => {
  return get(currentWorkspaceAtom).admin_extend ?? false
})

export const tenantExtendAtom = atom((get) => {
  return get(currentWorkspaceAtom).tenant_extend ?? false
})

/** 超管 + 超管租户双权限位（模板同步入口、应用中心管理操作等使用） */
export const useExtendPermissions = () => {
  const adminExtend = useAtomValue(adminExtendAtom)
  const tenantExtend = useAtomValue(tenantExtendAtom)
  return { adminExtend, tenantExtend }
}
