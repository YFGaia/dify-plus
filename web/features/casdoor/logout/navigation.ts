import { zCasdoorLogoutResponse } from '@dify/contracts/api/console/auth/zod.gen'
import { zPostLogoutResponse } from '@dify/contracts/api/console/logout/zod.gen'
import { zCasdoorNavigationResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { API_PREFIX } from '@/config'

/** The server authorizes the workflow through an HttpOnly Cookie, not this path. */
export function logoutHandoffDestination(data: unknown) {
  const handoff = zCasdoorNavigationResponse.strict().parse(data)
  if (
    !/^\/console\/api\/auth\/casdoor\/logout\/[\w-]{42}[AEIMQUYcgkosw048]$/.test(
      handoff.handoff_path,
    )
  )
    throw new Error('Logout navigation unavailable.')
  const base = new URL(API_PREFIX, window.location.origin)
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)
  if (
    (base.protocol !== 'https:' && !(base.protocol === 'http:' && loopback)) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    base.pathname.replace(/\/+$/, '') !== '/console/api'
  )
    throw new Error('Logout navigation unavailable.')
  return new URL(handoff.handoff_path, base.origin).href
}

const continuation = zCasdoorLogoutResponse
  .extend({ handoff: zCasdoorNavigationResponse.strict().nullish() })
  .strict()

export function parseRPLogoutContinuation(data: unknown) {
  const result = continuation.parse(data)
  if (result.status === 'local_only') {
    if (result.handoff != null) throw new Error('Logout navigation unavailable.')
    return null
  }
  if (!result.handoff) throw new Error('Logout navigation unavailable.')
  return logoutHandoffDestination(result.handoff)
}

/** Invalid optional metadata preserves the already successful local sign-out. */
export function parseLogoutDestination(data: unknown) {
  const response = zPostLogoutResponse
    .extend({ casdoor_logout: continuation.nullish() })
    .strict()
    .safeParse(data)
  if (!response.success || !response.data.casdoor_logout) return null
  try {
    return parseRPLogoutContinuation(response.data.casdoor_logout)
  } catch {
    return null
  }
}
