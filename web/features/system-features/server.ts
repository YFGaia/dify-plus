import { dehydrate } from '@tanstack/react-query'
import { cache } from 'react'
import { getQueryClient } from '@/app/get-query-client'
import { connection } from '@/next/server'
import { consoleQuery } from '@/service/console'
import { parseSystemFeaturesSnapshot } from './snapshot'
import 'server-only'

const getRequestQueryClient = cache(getQueryClient)

const systemFeaturesServerQueryOptions = () => {
  const options = consoleQuery.systemFeatures.get.queryOptions({ staleTime: 'static' })
  return {
    ...options,
    queryFn: async (context: Parameters<typeof options.queryFn>[0]) =>
      parseSystemFeaturesSnapshot(await options.queryFn(context)),
  }
}

export const getOptionalSystemFeatures = async () => {
  await connection()
  const queryClient = getRequestQueryClient()
  const queryOptions = systemFeaturesServerQueryOptions()
  const queryState = queryClient.getQueryState(queryOptions.queryKey)

  if (queryState?.status === 'error' && queryState.data === undefined) return undefined

  return queryClient.fetchQuery(queryOptions).catch(() => undefined)
}

export const getSystemFeatures = async () => {
  await connection()
  return getRequestQueryClient().fetchQuery(systemFeaturesServerQueryOptions())
}

export const dehydrateSystemFeatures = () => dehydrate(getRequestQueryClient())
