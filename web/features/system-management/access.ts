'use client'

import { zSystemManagementPermissionsResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { atom, useAtomValue } from 'jotai'
import { atomWithQuery } from 'jotai-tanstack-query'
import { currentWorkspaceIdAtom, isCurrentWorkspaceManagerAtom } from '@/context/workspace-state'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { consoleQuery } from '@/service/console'

// Authentication owns fetching the profile. Observe that existing cache without
// starting a second authentication request from each management surface.
const accountProfileAtom = atomWithQuery(() => ({
  ...userProfileQueryOptions(),
  enabled: false,
}))

const managementPermissionsAtom = atomWithQuery((get) => {
  const accountId = get(accountProfileAtom).data?.profile.id
  const workspaceId = get(currentWorkspaceIdAtom)
  const operation = consoleQuery.systemManageExtend.permissions.get
  return operation.queryOptions({
    queryKey: [...operation.key(), { accountId, workspaceId }],
    enabled: !!accountId && !!workspaceId && get(isCurrentWorkspaceManagerAtom),
    context: { silent: true },
    retry: false,
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    select: (data) => zSystemManagementPermissionsResponse.parse(data),
  })
})

const managementAccessAtom = atom((get) => {
  const accountId = get(accountProfileAtom).data?.profile.id
  const workspaceId = get(currentWorkspaceIdAtom)
  const isManager = get(isCurrentWorkspaceManagerAtom)
  const permissions = get(managementPermissionsAtom)
  const parsed = zSystemManagementPermissionsResponse.safeParse(permissions.data)
  const canManageSystem =
    !!accountId &&
    !!workspaceId &&
    isManager &&
    permissions.isSuccess &&
    !permissions.isError &&
    permissions.isFetchedAfterMount &&
    !permissions.isPlaceholderData &&
    parsed.success &&
    parsed.data.can_manage_system === true &&
    parsed.data.workspace_id === workspaceId &&
    parsed.data.account_id === accountId

  return {
    canManageSystem,
    isPending:
      !canManageSystem &&
      isManager &&
      !!accountId &&
      !!workspaceId &&
      (permissions.isPending || permissions.isFetching),
    error: permissions.error,
    refetch: permissions.refetch,
  }
})

export function useSystemManagementAccess() {
  return useAtomValue(managementAccessAtom)
}
