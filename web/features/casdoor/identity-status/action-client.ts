import type { QueryClient } from '@tanstack/react-query'
import {
  zCasdoorIdentityActionsResponse,
  zCasdoorIdentityUnlinkedResponse,
  zCasdoorNavigationResponse,
} from '@dify/contracts/api/console/account/zod.gen'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { consoleClient, consoleQuery } from '@/service/console'
import { parseIdentityNavigation } from '../identity-navigation'

export function identitySourceGuard(client: QueryClient) {
  const read = () => {
    const state = client.getQueryState(userProfileQueryOptions().queryKey)
    return state?.status === 'error' ? undefined : state?.data?.profile?.id
  }
  const anchor = read()
  return {
    anchor,
    check: () => {
      if (typeof anchor !== 'string' || !anchor.trim() || read() !== anchor)
        throw new Error('casdoor_identity_context_changed')
    },
  }
}

export function identityActionsQueryOptions(client: QueryClient) {
  const source = identitySourceGuard(client)
  return consoleQuery.account.casdoorIdentity.actions.get.queryOptions({
    queryKey: [
      ...consoleQuery.account.casdoorIdentity.actions.get.queryKey(),
      { profileAnchor: source.anchor ?? null },
    ],
    enabled: source.anchor !== undefined,
    context: { silent: true },
    retry: false,
    staleTime: 0,
    gcTime: 0,
    queryFn: async ({ signal }) => {
      source.check()
      const data = await consoleClient.account.casdoorIdentity.actions.get(undefined, {
        signal,
        context: { silent: true },
      })
      const result = zCasdoorIdentityActionsResponse.strict().parse(data)
      if (
        (result.link && (result.reauthenticate || result.unlink)) ||
        (result.unlink && !result.reauthenticate)
      )
        throw new Error('casdoor_identity_invalid_response')
      source.check()
      return result
    },
  })
}

export function identityLinkMutationOptions(client: QueryClient, kind: 'link' | 'reauthenticate') {
  const source = identitySourceGuard(client)
  return consoleQuery.account.casdoorIdentity[kind].post.mutationOptions({
    context: { silent: true },
    retry: false,
    gcTime: 0,
    mutationFn: async () => {
      source.check()
      const data = await consoleClient.account.casdoorIdentity[kind].post(
        { body: {} },
        { context: { silent: true } },
      )
      parseIdentityNavigation(data)
      source.check()
      return zCasdoorNavigationResponse.strict().parse(data)
    },
  })
}

export function identityUnlinkMutationOptions(client: QueryClient) {
  const source = identitySourceGuard(client)
  return consoleQuery.account.casdoorIdentity.unlink.post.mutationOptions({
    context: { silent: true },
    retry: false,
    gcTime: 0,
    mutationFn: async () => {
      source.check()
      const data = await consoleClient.account.casdoorIdentity.unlink.post(
        { body: {} },
        { context: { silent: true } },
      )
      const result = zCasdoorIdentityUnlinkedResponse.strict().parse(data)
      source.check()
      return result
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: consoleQuery.account.casdoorIdentity.get.key() }),
        client.invalidateQueries({
          queryKey: consoleQuery.account.casdoorIdentity.actions.get.key(),
        }),
      ])
    },
  })
}
