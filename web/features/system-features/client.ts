import { consoleQuery } from '@/service/console'
import { parseLoginConfig } from './extend'
import { parseSystemFeaturesSnapshot } from './snapshot'

export const systemFeaturesQueryOptions = () => {
  const options = consoleQuery.systemFeatures.get.queryOptions({
    staleTime: Infinity,
  })
  return {
    ...options,
    queryFn: async (context: Parameters<typeof options.queryFn>[0]) =>
      parseSystemFeaturesSnapshot(await options.queryFn(context)),
  }
}

export type LoginConfigIdentity =
  | { accountId: null; workspaceId: null }
  | { accountId: string; workspaceId: string }

// Consumers supply their current Console identity; anonymous sign-in uses nulls.
// Config changes invalidate consoleQuery.loginConfig.get.key(). Logout already
// clears the QueryClient. Never hydrate this browser-only, bootstrap-bound query.
export const loginConfigQueryOptions = (identity: LoginConfigIdentity) => {
  const options = consoleQuery.loginConfig.get.queryOptions({
    queryKey: [...consoleQuery.loginConfig.get.queryKey(), identity],
    staleTime: 0,
    gcTime: 0,
    retry: false,
  })
  return {
    ...options,
    queryFn: async (context: Parameters<typeof options.queryFn>[0]) => {
      if (typeof window === 'undefined')
        throw new Error('Login configuration is only available in the browser')
      return parseLoginConfig(await options.queryFn(context))
    },
  }
}
