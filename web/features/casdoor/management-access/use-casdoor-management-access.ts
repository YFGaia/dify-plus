'use client'

import { zCasdoorPermissionsResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { useQuery } from '@tanstack/react-query'
import { useSystemManagementAccess } from '@/features/system-management/access'
import { consoleQuery } from '@/service/console'

// All mounted entry points observe the generated key; logout clears this cache.
export function useCasdoorManagementAccess() {
  const { canManageSystem, isPending } = useSystemManagementAccess()
  const permissions = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.permissions.get.queryOptions({
      context: { silent: true },
      retry: false,
      enabled: canManageSystem,
      staleTime: 0,
    }),
  )
  const raw = permissions.data
  const canManageCasdoor =
    canManageSystem &&
    permissions.isSuccess &&
    !permissions.isError &&
    raw != null &&
    Object.hasOwn(raw, 'can_manage_casdoor') &&
    raw.can_manage_casdoor === true &&
    zCasdoorPermissionsResponse.safeParse(raw).success

  return {
    canManageCasdoor,
    error: permissions.error,
    refetch: permissions.refetch,
    isPending: isPending || (canManageSystem && permissions.isPending),
  }
}
