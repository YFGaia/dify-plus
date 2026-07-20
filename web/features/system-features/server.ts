import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'
import { queryOptions } from '@tanstack/react-query'
import { IS_CLOUD_EDITION } from '@/config'
import {
  getServerConsoleClientContext,
  serverConsoleClient,
  serverConsoleQuery,
} from '@/service/server'
import { cloudSystemFeatures, defaultSystemFeatures } from './config'

export const serverSystemFeaturesQueryOptions = () => {
  const queryKey = serverConsoleQuery.systemFeatures.get.queryKey()

  if (IS_CLOUD_EDITION) {
    return queryOptions<GetSystemFeaturesResponse>({
      queryKey,
      queryFn: async () => cloudSystemFeatures,
      staleTime: 'static',
    })
  }

  return queryOptions<GetSystemFeaturesResponse>({
    queryKey,
    queryFn: async () => {
      try {
        const res = await serverConsoleClient.systemFeatures.get(undefined, {
          context: await getServerConsoleClientContext(),
        })
        // extend: CVE-2025-63387 — the fork replaces /console/api/system-features with a
        // health-check stub ({"ping":true}); the real payload is served via the JWT-gated
        // /login_config (see client.ts). The stub returns 200, so the catch below never
        // fires for it. Guard on shape: without `branding` the payload is unusable for
        // rendering (main-nav reads systemFeatures.branding.enabled) and would crash SSR.
        // Fall back to defaults; the client refetches the real values through login_config.
        if (!res || typeof res !== 'object' || !('branding' in res))
          return defaultSystemFeatures
        return res
      } catch (err) {
        console.error('[systemFeatures] server fetch failed', err)
        return defaultSystemFeatures
      }
    },
  })
}
