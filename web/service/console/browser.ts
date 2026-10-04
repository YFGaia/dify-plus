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

function publicCasdoorOperation(path: readonly string[]) {
  if (
    path.length === 4 &&
    path[0] === 'auth' &&
    path[1] === 'casdoor' &&
    path[3] === 'get' &&
    (path[2] === 'result' || path[2] === 'display')
  )
    return path[2]
}

function privateCasdoorIdentityOperation(path: readonly string[]) {
  return (
    path.length === 3 && path[0] === 'account' && path[1] === 'casdoorIdentity' && path[2] === 'get'
  )
}

function createBrowserLink(contract: AnyContractRouter): ClientLink<ConsoleClientContext> {
  return new OpenAPILink<ConsoleClientContext>(contract, {
    url: () => new URL(API_PREFIX, window.location.origin),
    fetch: async (input, init, options, path) => {
      let requestInit: RequestInit = options.context.keepalive ? { ...init, keepalive: true } : init
      const normalizedURL = normalizeConsoleOpenAPIURL(input.url)
      let normalizedRequest =
        normalizedURL === input.url ? input : new Request(normalizedURL, input)
      const casdoorOperation = publicCasdoorOperation(path)
      if (casdoorOperation) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        if (
          normalizedRequest.method !== 'GET' ||
          target.origin !== prefix.origin ||
          target.pathname !==
            `${prefix.pathname.replace(/\/$/, '')}/auth/casdoor/${casdoorOperation}`
        )
          throw new Error('Invalid public sign-in request.')
        const dispatchRequest = new Request(normalizedRequest, {
          ...requestInit,
          credentials: 'include',
          cache: 'no-store',
          redirect: 'error',
        })
        if (casdoorOperation === 'result') {
          const beforeRequest = options.context.beforeCasdoorResultRequest
          if (!beforeRequest) throw new Error('Sign-in result request is no longer available.')
          // The owner checks its live URL and removes the handoff immediately before dispatch.
          beforeRequest()
        }
        const response = await globalThis.fetch(dispatchRequest)
        if (response.status >= 300 && response.status < 400)
          throw new Error('Unexpected public sign-in redirect.')
        return response
      }
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
      const privateCasdoorIdentity = privateCasdoorIdentityOperation(path)
      if (privateCasdoorIdentity) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        if (
          normalizedRequest.method !== 'GET' ||
          target.origin !== prefix.origin ||
          target.pathname !== `${prefix.pathname.replace(/\/$/, '')}/account/casdoor-identity`
        )
          throw new Error('Invalid account identity request.')
        requestInit = { ...requestInit, cache: 'no-store' }
      }
      // A 403 is propagated unchanged. A later query starts with a fresh bootstrap.
      return request(normalizedURL, requestInit, {
        fetchCompat: true,
        request: normalizedRequest,
        silent: privateCasdoorIdentity || options.context.silent,
      })
    },
    interceptors: [
      onError((error, options) => {
        if (!publicCasdoorOperation(options.path) && !privateCasdoorIdentityOperation(options.path))
          console.error(error)
      }),
    ],
  })
}

export const consoleBrowserLink = createConsoleContractLink(createBrowserLink)
