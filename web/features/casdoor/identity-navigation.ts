import { zCasdoorNavigationResponse } from '@dify/contracts/api/console/account/zod.gen'
import { API_PREFIX } from '@/config'

export function parseIdentityNavigation(data: unknown) {
  const handoff = zCasdoorNavigationResponse.strict().parse(data)
  if (!/^\/console\/api\/auth\/casdoor\/identity\/[\w-]{43}$/.test(handoff.handoff_path))
    throw new Error('casdoor_identity_invalid_navigation')
  const base = new URL(API_PREFIX, window.location.origin)
  if (
    !['http:', 'https:'].includes(base.protocol) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    base.pathname.replace(/\/+$/, '') !== '/console/api'
  )
    throw new Error('casdoor_identity_invalid_navigation')
  return new URL(handoff.handoff_path, base.origin).href
}
