import type {
  CasdoorLocalMembershipMutationPayload,
  CasdoorLocalMembershipReviewPayload,
  GetSystemManageExtendIntegrationCasdoorLocalMembershipTargetsData,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { QueryClient } from '@tanstack/react-query'
import {
  zCasdoorLocalMembershipInspectionResponse,
  zCasdoorLocalMembershipListResponse,
  zCasdoorLocalMembershipMutationResponse,
  zCasdoorLocalMembershipReviewResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { consoleClient, consoleQuery } from '@/service/console'
import { identitySourceGuard } from '../identity-status/action-client'

const local = consoleClient.systemManageExtend.integration.casdoor.localMembership
const query = consoleQuery.systemManageExtend.integration.casdoor.localMembership

export function localTargetsOptions(
  client: QueryClient,
  pagination: NonNullable<
    GetSystemManageExtendIntegrationCasdoorLocalMembershipTargetsData['query']
  > = {},
) {
  const source = identitySourceGuard(client)
  return query.targets.get.queryOptions({
    input: { query: pagination },
    queryKey: [
      ...query.targets.get.queryKey({ input: { query: pagination } }),
      { source: source.anchor ?? null },
    ],
    enabled: typeof source.anchor === 'string',
    context: { silent: true },
    retry: false,
    staleTime: 0,
    gcTime: 0,
    queryFn: async ({ signal }) => {
      source.check()
      const data = await local.targets.get(
        { query: pagination },
        { signal, context: { silent: true } },
      )
      const result = zCasdoorLocalMembershipListResponse.strict().parse(data)
      if (result.has_more && (!result.next_identity_id || !result.next_workspace_id))
        throw new Error('casdoor_local_invalid_navigation')
      source.check()
      return result
    },
  })
}

export function localInspectionOptions(
  client: QueryClient,
  identity_id: string,
  workspace_id: string,
) {
  const source = identitySourceGuard(client)
  const input = { query: { identity_id, workspace_id } }
  return query.get.queryOptions({
    input,
    queryKey: [
      ...query.get.queryKey({ input }),
      { source: source.anchor ?? null, identity_id, workspace_id },
    ],
    context: { silent: true },
    retry: false,
    staleTime: 0,
    gcTime: 0,
    queryFn: async ({ signal }) => {
      source.check()
      const result = zCasdoorLocalMembershipInspectionResponse
        .strict()
        .parse(await local.get(input, { signal, context: { silent: true } }))
      if (
        result.identity_id !== identity_id ||
        result.workspace_id !== workspace_id ||
        !result.local_no_intent
      )
        throw new Error('casdoor_local_context_changed')
      source.check()
      return result
    },
  })
}

export function localReviewOptions(client: QueryClient) {
  const source = identitySourceGuard(client)
  return query.review.post.mutationOptions({
    context: { silent: true },
    retry: false,
    gcTime: 0,
    mutationFn: async (input: { body: CasdoorLocalMembershipReviewPayload }) => {
      source.check()
      const result = zCasdoorLocalMembershipReviewResponse
        .strict()
        .parse(await local.review.post(input, { context: { silent: true } }))
      if (result.etag !== input.body.etag || result.operation !== input.body.operation)
        throw new Error('casdoor_local_context_changed')
      source.check()
      return result
    },
  })
}

export function localMutationOptions(client: QueryClient, operation: 'release' | 'adopt') {
  const source = identitySourceGuard(client)
  return query[operation].post.mutationOptions({
    context: { silent: true },
    retry: false,
    gcTime: 0,
    mutationFn: async (input: { body: CasdoorLocalMembershipMutationPayload }) => {
      source.check()
      const result = zCasdoorLocalMembershipMutationResponse
        .strict()
        .parse(await local[operation].post(input, { context: { silent: true } }))
      if (result.status !== (operation === 'release' ? 'released' : 'adopted'))
        throw new Error('casdoor_local_context_changed')
      source.check()
      return result
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: query.key() }),
        client.invalidateQueries({ queryKey: consoleQuery.account.casdoorIdentity.get.key() }),
        client.invalidateQueries({
          queryKey: consoleQuery.account.casdoorIdentity.actions.get.key(),
        }),
      ])
    },
  })
}
