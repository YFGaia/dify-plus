import {
  zCasdoorNavigationResponse,
  zCasdoorTestLoginResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { API_PREFIX } from '@/config'

export function parseDiagnosticStart(data: unknown) {
  const response = zCasdoorTestLoginResponse
    .extend({
      handoff: zCasdoorNavigationResponse.strict().nullish(),
    })
    .strict()
    .parse(data)
  if (response.status === 'blocked') {
    if (!response.reason || response.handoff != null)
      throw new Error('Invalid diagnostic response.')
    return { response, destination: null }
  }
  if (response.reason != null || !response.handoff) throw new Error('Invalid diagnostic response.')
  const handoff = zCasdoorNavigationResponse.strict().parse(response.handoff)
  if (!/^\/console\/api\/auth\/casdoor\/diagnostic\/[\w-]{43}$/.test(handoff.handoff_path))
    throw new Error('Invalid diagnostic navigation.')
  const base = new URL(API_PREFIX, window.location.origin)
  if (
    !['https:', 'http:'].includes(base.protocol) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    base.pathname.replace(/\/+$/, '') !== '/console/api'
  )
    throw new Error('Diagnostic navigation unavailable.')
  return { response, destination: new URL(handoff.handoff_path, base.origin).href }
}
