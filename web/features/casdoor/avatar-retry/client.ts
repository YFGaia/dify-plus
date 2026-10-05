import type {
  CasdoorRetrySyncPayload,
  GetSystemManageExtendIntegrationCasdoorSyncRetryTargetsData,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { QueryClient } from '@tanstack/react-query'
import {
  zCasdoorAvatarRetryResponse,
  zCasdoorAvatarRetryTargetsResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { consoleClient, consoleQuery } from '@/service/console'
import { captureManagerSource } from '../manager-source'

export function avatarRetryTargetsOptions(
  client: QueryClient,
  pagination: NonNullable<
    GetSystemManageExtendIntegrationCasdoorSyncRetryTargetsData['query']
  > = {},
) {
  const source = captureManagerSource(client)
  const operation = consoleQuery.systemManageExtend.integration.casdoor.sync.retryTargets.get
  return operation.queryOptions({
    input: { query: pagination },
    queryKey: [...operation.queryKey({ input: { query: pagination } }), { source: source.key }],
    enabled: source.key !== null,
    context: { silent: true },
    retry: false,
    staleTime: 0,
    gcTime: 0,
    queryFn: async ({ signal }) => {
      source.check()
      const result = zCasdoorAvatarRetryTargetsResponse
        .strict()
        .parse(
          await consoleClient.systemManageExtend.integration.casdoor.sync.retryTargets.get(
            { query: pagination },
            { signal, context: { silent: true } },
          ),
        )
      source.check()
      if (
        result.has_more !== (result.next_after !== null) ||
        (pagination.after !== undefined &&
          result.next_after !== null &&
          result.next_after.toLowerCase() <= pagination.after.toLowerCase()) ||
        new Set(result.targets.map((target) => target.intent_id)).size !== result.targets.length
      )
        throw new Error('casdoor_avatar_invalid_navigation')
      return result
    },
  })
}

export function avatarRetryOptions(client: QueryClient) {
  const source = captureManagerSource(client)
  return consoleQuery.systemManageExtend.integration.casdoor.sync.retry.post.mutationOptions({
    context: { silent: true },
    mutationFn: async (input: { body: CasdoorRetrySyncPayload }) => {
      source.check()
      const result = zCasdoorAvatarRetryResponse.strict().parse(
        await consoleClient.systemManageExtend.integration.casdoor.sync.retry.post(input, {
          context: { silent: true },
        }),
      )
      source.check()
      if (result.intent_id !== input.body.intent_id)
        throw new Error('casdoor_avatar_retry_unconfirmed')
      return result
    },
  })
}
