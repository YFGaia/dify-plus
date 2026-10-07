import type { QueryClient } from '@tanstack/react-query'
import { zCasdoorSessionResponse } from '@dify/contracts/api/console/auth/zod.gen'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { consoleClient, consoleQuery } from '@/service/console'

export function casdoorSessionQueryOptions(client: QueryClient, profileAccountId?: string) {
  const profileKey = userProfileQueryOptions().queryKey
  const currentAccount = () => client.getQueryData(profileKey)?.profile?.id
  const anchor = profileAccountId ?? currentAccount()
  return consoleQuery.auth.casdoor.session.get.queryOptions({
    queryKey: [...consoleQuery.auth.casdoor.session.get.key(), { account: anchor ?? null }],
    enabled:
      typeof anchor === 'string' &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(anchor),
    staleTime: 0,
    gcTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      if (!anchor || currentAccount() !== anchor) throw new Error('casdoor_session_changed')
      const data = await consoleClient.auth.casdoor.session.get(
        {},
        { signal, context: { silent: true } },
      )
      const parsed = zCasdoorSessionResponse.strict().parse(data)
      if (currentAccount() !== anchor) throw new Error('casdoor_session_changed')
      // This flag only changes explanatory copy. The local logout owner and
      // private Cookie-bound workflow still decide whether navigation exists.
      if (
        parsed.source === 'local_only' &&
        (parsed.verified || parsed.expires_at != null || parsed.rp_logout_available)
      )
        throw new Error('casdoor_session_unavailable')
      if (
        parsed.source === 'casdoor' &&
        (!parsed.verified ||
          !parsed.expires_at ||
          !Number.isFinite(Date.parse(parsed.expires_at)) ||
          Date.parse(parsed.expires_at) <= Date.now())
      )
        throw new Error('casdoor_session_unavailable')
      return parsed
    },
  })
}
