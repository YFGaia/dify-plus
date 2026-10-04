'use client'

import { zCasdoorPermissionsResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { useQuery } from '@tanstack/react-query'
import { consoleQuery } from '@/service/console'

// All mounted entry points observe the generated key; logout clears this cache.
export function useCasdoorManagementAccess() {
  const permissions = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.permissions.get.queryOptions({
      context: { silent: true },
      retry: false,
      staleTime: 0,
    }),
  )
  const raw = permissions.data
  const canManageCasdoor =
    permissions.isSuccess &&
    !permissions.isError &&
    raw != null &&
    Object.hasOwn(raw, 'can_manage_casdoor') &&
    raw.can_manage_casdoor === true &&
    zCasdoorPermissionsResponse.safeParse(raw).success

  return { canManageCasdoor, isPending: permissions.isPending }
}
