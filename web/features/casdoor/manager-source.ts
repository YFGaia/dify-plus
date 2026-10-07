import type { QueryClient } from '@tanstack/react-query'
import type { UserProfileWithMeta } from '@/features/account-profile/client'
import { hashKey } from '@tanstack/react-query'
import { useSyncExternalStore } from 'react'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { identitySourceGuard } from './identity-status/action-client'

const identities = new WeakMap<object, number>()
let nextIdentity = 0

function sourceKey(client: QueryClient) {
  const queryKey = userProfileQueryOptions().queryKey
  const query = client.getQueryCache().find({ queryKey, exact: true })
  const state = client.getQueryState<UserProfileWithMeta>(queryKey)
  const account = state?.data?.profile.id
  if (
    !query ||
    !state ||
    state.status !== 'success' ||
    typeof account !== 'string' ||
    !account.trim()
  )
    return null
  if (!identities.has(query)) identities.set(query, ++nextIdentity)
  // The update counter also catches A → B → A within one clock tick.
  return `${identities.get(query)}:${state.dataUpdateCount}:${state.dataUpdatedAt}:${state.errorUpdateCount}:${account}`
}

export function captureManagerSource(client: QueryClient) {
  const source = identitySourceGuard(client)
  const key = sourceKey(client)
  return {
    key,
    check: () => {
      if (key === null || sourceKey(client) !== key)
        throw new Error('casdoor_manager_source_changed')
      source.check()
    },
  }
}

export function useManagerSourceKey(client: QueryClient) {
  const profileHash = hashKey(userProfileQueryOptions().queryKey)
  return useSyncExternalStore(
    (notify) =>
      client.getQueryCache().subscribe((event) => {
        if (event.query.queryHash === profileHash) notify()
      }),
    () => sourceKey(client),
    () => null,
  )
}
