import type { ClientLink } from '@orpc/client'
import type { AnyContractRouter, ContractRouterClient } from '@orpc/contract'
import type { ConsoleClientContext } from './index'
import { createORPCClient, onError } from '@orpc/client'
import { OpenAPILink } from '@orpc/openapi-client/fetch'
import { API_PREFIX } from '@/config'
// oxlint-disable-next-line no-restricted-imports -- The Console HTTP adapter must preserve existing auth refresh, CSRF, and error handling.
import { request } from '../base'
import { createConsoleContractLink } from './contract-loader'
import { normalizeConsoleOpenAPIURL } from './openapi-url'

function createBrowserLink(contract: AnyContractRouter): ClientLink<ConsoleClientContext> {
  return new OpenAPILink<ConsoleClientContext>(contract, {
    url: () => new URL(API_PREFIX, window.location.origin),
    fetch: async (input, init, options, path) => {
      let requestInit: RequestInit = options.context.keepalive ? { ...init, keepalive: true } : init
      const normalizedURL = normalizeConsoleOpenAPIURL(input.url)
      let normalizedRequest =
        normalizedURL === input.url ? input : new Request(normalizedURL, input)
      if (path[0] === 'loginConfigBootstrap' || path[0] === 'loginConfig')
        requestInit = { ...requestInit, credentials: 'include', cache: 'no-store' }
      if (path[0] === 'loginConfig') {
        // Keep the token local to this request: no account switch or SSR request can reuse it.
        const { contract: bootstrapContract } =
          await import('@dify/contracts/api/console/login-config-bootstrap/orpc.gen')
        const bootstrapClient = createORPCClient<
          ContractRouterClient<typeof bootstrapContract, ConsoleClientContext>
        >(createBrowserLink(bootstrapContract))
        const { zGetLoginConfigBootstrapResponse } =
          await import('@dify/contracts/api/console/login-config-bootstrap/zod.gen')
        const bootstrap = zGetLoginConfigBootstrapResponse.parse(
          await bootstrapClient.loginConfigBootstrap.get(undefined, options),
        )
        if (!bootstrap.ok || !bootstrap.token)
          throw new Error('Login configuration bootstrap failed.')
        const headers = new Headers(normalizedRequest.headers)
        headers.set('X-Login-Config-Token', bootstrap.token)
        normalizedRequest = new Request(normalizedRequest, {
          headers,
        })
      }
      // A 403 is propagated unchanged. A later query starts with a fresh bootstrap.
      return request(normalizedURL, requestInit, {
        fetchCompat: true,
        request: normalizedRequest,
        silent: options.context.silent,
      })
    },
    interceptors: [onError((error) => console.error(error))],
  })
}

export const consoleBrowserLink = createConsoleContractLink(createBrowserLink)
