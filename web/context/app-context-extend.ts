'use client'

// Extend: permission bits come from the generated current workspace summary.
// Until a valid summary is available, the initial state grants neither bit.
import { atom, useAtomValue } from 'jotai'
import { currentWorkspaceAtom } from './workspace-state'

export const adminExtendAtom = atom((get) => {
  return get(currentWorkspaceAtom).admin_extend
})

export const tenantExtendAtom = atom((get) => {
  return get(currentWorkspaceAtom).tenant_extend
})

/** 超管 + 超管租户双权限位（模板同步入口、应用中心管理操作等使用） */
export const useExtendPermissions = () => {
  const adminExtend = useAtomValue(adminExtendAtom)
  const tenantExtend = useAtomValue(tenantExtendAtom)
  return { adminExtend, tenantExtend }
}
