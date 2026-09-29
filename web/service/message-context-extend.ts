import type {
  DeleteMessageContextData,
  GetMessageContextData,
} from '@dify/contracts/api/console/message/types.gen'
import type { ContractRouterClient } from '@orpc/contract'
import { contract } from '@dify/contracts/api/console/message/orpc.gen'
import {
  zDeleteMessageContextQuery,
  zDeleteMessageContextResponse2,
  zGetMessageContextQuery,
  zGetMessageContextResponse,
} from '@dify/contracts/api/console/message/zod.gen'
import { createORPCClient } from '@orpc/client'
import { OpenAPILink } from '@orpc/openapi-client/fetch'
import Cookies from 'js-cookie'
import { API_PREFIX, CSRF_COOKIE_NAME, CSRF_HEADER_NAME } from '@/config'

type ContextRequest = { csrfToken: string }

// WebApps may be anonymous. This narrow generated client deliberately avoids the
// shared Console adapter's token refresh and login redirect on HTTP 401.
const client = createORPCClient<ContractRouterClient<typeof contract, ContextRequest>>(
  new OpenAPILink<ContextRequest>(contract, {
    url: () => new URL(API_PREFIX, window.location.origin),
    fetch: (request, init, options) => {
      const headers = new Headers(request.headers)
      headers.set(CSRF_HEADER_NAME, options.context.csrfToken)
      return fetch(new Request(request, { headers }), {
        ...init,
        credentials: 'include',
        cache: 'no-store',
        redirect: 'error',
      })
    },
  }),
)

export const hasConsoleContextSession = () => !!Cookies.get(CSRF_COOKIE_NAME())

export async function messageContextList(query: GetMessageContextData['query']) {
  const csrfToken = Cookies.get(CSRF_COOKIE_NAME())
  if (!csrfToken) return []
  const response = await client.message.context.get(
    { query: zGetMessageContextQuery.parse(query) },
    { context: { csrfToken } },
  )
  return zGetMessageContextResponse.parse(response)
}

export async function deleteMessageContext(query: DeleteMessageContextData['query']) {
  const csrfToken = Cookies.get(CSRF_COOKIE_NAME())
  if (!csrfToken) return undefined
  const response = zDeleteMessageContextResponse2.parse(
    await client.message.context.delete(
      { query: zDeleteMessageContextQuery.parse(query) },
      { context: { csrfToken } },
    ),
  )
  if (response !== 'ok') throw new Error('Message context deletion failed.')
  return response
}
