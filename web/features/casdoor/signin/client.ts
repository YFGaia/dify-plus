import { zCasdoorDisplayResponse } from '@dify/contracts/api/console/auth/zod.gen'
import { consoleQuery } from '@/service/console'

function parseDisplay(raw: unknown) {
  if (
    typeof raw !== 'object' ||
    raw === null ||
    Array.isArray(raw) ||
    Reflect.ownKeys(raw).length !== 3 ||
    !Object.hasOwn(raw, 'button_text') ||
    !Object.hasOwn(raw, 'enabled') ||
    !Object.hasOwn(raw, 'start_path') ||
    !('button_text' in raw) ||
    typeof raw.button_text !== 'string' ||
    !('enabled' in raw) ||
    typeof raw.enabled !== 'boolean' ||
    !('start_path' in raw) ||
    typeof raw.start_path !== 'string'
  )
    throw new Error('Invalid sign-in display.')

  return zCasdoorDisplayResponse.strict().parse(raw)
}

export const casdoorDisplayQueryOptions = (enabled: boolean) =>
  consoleQuery.auth.casdoor.display.get.queryOptions({
    enabled: typeof window !== 'undefined' && enabled,
    select: parseDisplay,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    throwOnError: false,
    context: { silent: true },
  })
