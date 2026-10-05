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
    path[0] === 'account' &&
    path[1] === 'casdoorIdentity' &&
    ((path.length === 3 && path[2] === 'get') ||
      (path.length === 4 &&
        ((path[2] === 'actions' && path[3] === 'get') ||
          (['link', 'reauthenticate', 'unlink'].includes(path[2]!) && path[3] === 'post'))))
  )
}

function privateCasdoorSessionOperation(path: readonly string[]) {
  return (
    path.length === 4 &&
    path[0] === 'auth' &&
    path[1] === 'casdoor' &&
    path[2] === 'session' &&
    path[3] === 'get'
  )
}

function anonymousRPLogoutOperation(path: readonly string[]) {
  return path.length === 5 && path.join('/') === 'auth/casdoor/logout/retry/post'
}

function privateRPDiagnosticOperation(path: readonly string[]) {
  return (
    path.length === 5 &&
    path[0] === 'systemManageExtend' &&
    path[1] === 'integration' &&
    path[2] === 'casdoor' &&
    ((path[3] === 'rpLogoutStatus' && path[4] === 'get') ||
      (path[3] === 'testRpLogout' && path[4] === 'post'))
  )
}

function privateCasdoorLocalMembershipOperation(path: readonly string[]) {
  return (
    path[0] === 'systemManageExtend' &&
    path[1] === 'integration' &&
    path[2] === 'casdoor' &&
    path[3] === 'localMembership' &&
    ((path.length === 5 && path[4] === 'get') ||
      (path.length === 6 &&
        ((path[4] === 'targets' && path[5] === 'get') ||
          (['review', 'release', 'adopt'].includes(path[4]!) && path[5] === 'post'))))
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
      if (anonymousRPLogoutOperation(path)) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        if (
          normalizedRequest.method !== 'POST' ||
          target.origin !== prefix.origin ||
          target.pathname !== `${prefix.pathname.replace(/\/$/, '')}/auth/casdoor/logout/retry` ||
          target.search ||
          target.hash
        )
          throw new Error('Logout continuation unavailable.')
        // Anonymous continuation never invokes the old refresh/login recovery.
        const response = await globalThis.fetch(
          new Request(normalizedRequest, {
            ...requestInit,
            credentials: 'include',
            cache: 'no-store',
            redirect: 'error',
          }),
        )
        if (response.status >= 300 && response.status < 400)
          throw new Error('Unexpected logout continuation redirect.')
        return response
      }
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
      const privateCasdoorSession = privateCasdoorSessionOperation(path)
      const privateLocalMembership = privateCasdoorLocalMembershipOperation(path)
      const privateRPDiagnostic = privateRPDiagnosticOperation(path)
      if (privateRPDiagnostic) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        const isRead = path[3] === 'rpLogoutStatus'
        if (
          normalizedRequest.method !== (isRead ? 'GET' : 'POST') ||
          target.origin !== prefix.origin ||
          target.hash ||
          target.pathname !==
            `${prefix.pathname.replace(/\/$/, '')}/system-manage-extend/integration/casdoor/${isRead ? 'rp-logout-status' : 'test-rp-logout'}`
        )
          throw new Error('Logout diagnostic unavailable.')
        requestInit = { ...requestInit, cache: 'no-store' }
      }
      if (privateLocalMembership) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        const suffix = path.length === 5 ? '' : `/${path[4]}`
        if (
          normalizedRequest.method !== (path.at(-1) === 'get' ? 'GET' : 'POST') ||
          target.origin !== prefix.origin ||
          target.pathname !==
            `${prefix.pathname.replace(/\/$/, '')}/system-manage-extend/integration/casdoor/local-membership${suffix}` ||
          target.hash !== ''
        )
          throw new Error('Invalid local membership request.')
        requestInit = { ...requestInit, cache: 'no-store' }
      }
      if (privateCasdoorSession) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        if (
          normalizedRequest.method !== 'GET' ||
          target.origin !== prefix.origin ||
          target.pathname !== `${prefix.pathname.replace(/\/$/, '')}/auth/casdoor/session` ||
          target.search !== '' ||
          target.hash !== ''
        )
          throw new Error('Invalid session source request.')
        requestInit = { ...requestInit, cache: 'no-store' }
      }
      if (privateCasdoorIdentity) {
        const prefix = new URL(API_PREFIX, window.location.origin)
        const target = new URL(normalizedRequest.url)
        const suffix = path.length === 3 ? '' : `/${path[2]}`
        if (
          normalizedRequest.method !== (path.at(-1) === 'get' ? 'GET' : 'POST') ||
          target.origin !== prefix.origin ||
          target.pathname !==
            `${prefix.pathname.replace(/\/$/, '')}/account/casdoor-identity${suffix}`
        )
          throw new Error('Invalid account identity request.')
        requestInit = { ...requestInit, cache: 'no-store' }
      }
      // A 403 is propagated unchanged. A later query starts with a fresh bootstrap.
      return request(normalizedURL, requestInit, {
        fetchCompat: true,
        request: normalizedRequest,
        silent:
          privateCasdoorIdentity ||
          privateCasdoorSession ||
          privateLocalMembership ||
          privateRPDiagnostic ||
          options.context.silent,
      })
    },
    interceptors: [
      onError((error, options) => {
        if (
          !publicCasdoorOperation(options.path) &&
          !privateCasdoorIdentityOperation(options.path) &&
          !privateCasdoorSessionOperation(options.path) &&
          !privateCasdoorLocalMembershipOperation(options.path) &&
          !anonymousRPLogoutOperation(options.path) &&
          !privateRPDiagnosticOperation(options.path)
        )
          console.error(error)
      }),
    ],
  })
}

export const consoleBrowserLink = createConsoleContractLink(createBrowserLink)
