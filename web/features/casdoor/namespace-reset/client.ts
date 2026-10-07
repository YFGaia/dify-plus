import type {
  CasdoorNamespaceResetMutationPayload,
  CasdoorResetNamespacePayload,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { QueryClient } from '@tanstack/react-query'
import {
  zCasdoorConfigurationResponse,
  zCasdoorNamespaceResetReviewResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { consoleClient, consoleQuery } from '@/service/console'
import { parseServerConfiguration } from '../configuration-form/configuration-draft'
import { captureManagerSource } from '../manager-source'

export function resetContext(data: unknown) {
  const result = parseServerConfiguration(data)
  if (!result || result.enabled || (!result.draft && !result.active)) return null
  const revisions = [result.draft, result.active].filter((revision) => revision != null)
  const namespace = revisions[0]?.namespace_id
  if (!namespace || revisions.some((revision) => revision.namespace_id !== namespace)) return null
  return { namespace_id: namespace, etag: result.etag }
}

function checkContext(client: QueryClient, namespace: string, etag: number) {
  const state = client.getQueryState(
    consoleQuery.systemManageExtend.integration.casdoor.get.queryKey(),
  )
  const context = resetContext(state?.data)
  if (
    state?.status !== 'success' ||
    state.fetchStatus !== 'idle' ||
    context?.namespace_id !== namespace ||
    context.etag !== etag
  )
    throw new Error('casdoor_reset_context_changed')
}

export function resetReviewOptions(client: QueryClient) {
  const source = captureManagerSource(client)
  const api = consoleClient.systemManageExtend.integration.casdoor.resetNamespace
  return consoleQuery.systemManageExtend.integration.casdoor.resetNamespace.review.post.mutationOptions(
    {
      context: { silent: true },
      mutationFn: async (input: { body: CasdoorResetNamespacePayload }) => {
        source.check()
        checkContext(client, input.body.namespace_id, input.body.etag)
        const result = zCasdoorNamespaceResetReviewResponse
          .strict()
          .parse(await api.review.post(input, { context: { silent: true } }))
        source.check()
        checkContext(client, input.body.namespace_id, input.body.etag)
        if (result.namespace_id !== input.body.namespace_id || result.etag !== input.body.etag)
          throw new Error('casdoor_reset_context_changed')
        return result
      },
    },
  )
}

export function namespaceResetOptions(client: QueryClient, namespace: string | null) {
  const source = captureManagerSource(client)
  return consoleQuery.systemManageExtend.integration.casdoor.resetNamespace.post.mutationOptions({
    context: { silent: true },
    mutationFn: async (input: { body: CasdoorNamespaceResetMutationPayload }) => {
      source.check()
      if (!namespace) throw new Error('casdoor_reset_context_changed')
      checkContext(client, namespace, input.body.etag)
      const result = zCasdoorConfigurationResponse.strict().parse(
        await consoleClient.systemManageExtend.integration.casdoor.resetNamespace.post(input, {
          context: { silent: true },
        }),
      )
      source.check()
      const context = resetContext(result)
      if (
        !context ||
        context.etag !== input.body.etag + 1 ||
        context.namespace_id === namespace ||
        result.active_revision_id !== null ||
        result.active !== null ||
        !result.draft ||
        result.draft.namespace_id !== context.namespace_id ||
        result.draft_revision_id !== result.draft.revision_id
      )
        throw new Error('casdoor_reset_unconfirmed')
      return result
    },
  })
}
