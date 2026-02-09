import type { ContractRouterClient } from '@orpc/contract'
import type { JsonifiedClient } from '@orpc/openapi-client'
import { createORPCClient, onError } from '@orpc/client'
import { OpenAPILink } from '@orpc/openapi-client/fetch'
import { createTanstackQueryUtils } from '@orpc/tanstack-query'
import {
  API_PREFIX,
  APP_VERSION,
  IS_MARKETPLACE,
  MARKETPLACE_API_PREFIX,
} from '@/config'
import {
  consoleRouterContract,
  marketplaceRouterContract,
} from '@/contract/router'
import { request } from './base'

// extend: CVE-2025-63387 跨域时 Cookie 可能为 None，用 Header 携带 JWT
let loginConfigToken: string | null = null
export function setLoginConfigToken(token: string | null) {
  loginConfigToken = token
}

const getMarketplaceHeaders = () => new Headers({
  'X-Dify-Version': !IS_MARKETPLACE ? APP_VERSION : '999.0.0',
})

const getConsoleHeaders = () => {
  const h = new Headers()
  if (loginConfigToken)
    h.set('X-Login-Config-Token', loginConfigToken)
  return h
}

const marketplaceLink = new OpenAPILink(marketplaceRouterContract, {
  url: MARKETPLACE_API_PREFIX,
  headers: () => (getMarketplaceHeaders()),
  fetch: (request, init) => {
    return globalThis.fetch(request, {
      ...init,
      cache: 'no-store',
    })
  },
  interceptors: [
    onError((error) => {
      console.error(error)
    }),
  ],
})

export const marketplaceClient: JsonifiedClient<ContractRouterClient<typeof marketplaceRouterContract>> = createORPCClient(marketplaceLink)
export const marketplaceQuery = createTanstackQueryUtils(marketplaceClient, { path: ['marketplace'] })

const consoleLink = new OpenAPILink(consoleRouterContract, {
  url: API_PREFIX,
  headers: () => getConsoleHeaders(),
  fetch: (input, init) => {
    return request(
      input.url,
      init,
      {
        fetchCompat: true,
        request: input,
      },
    )
  },
  interceptors: [
    onError((error) => {
      console.error(error)
    }),
  ],
})

export const consoleClient: JsonifiedClient<ContractRouterClient<typeof consoleRouterContract>> = createORPCClient(consoleLink)
export const consoleQuery = createTanstackQueryUtils(consoleClient, { path: ['console'] })
