import type {
  GetAccountCasdoorIdentityData,
  GetAccountCasdoorIdentityResponse,
} from '@dify/contracts/api/console/account/types.gen'
import type { QueryClient } from '@tanstack/react-query'
import {
  zCasdoorSelfActionsResponse,
  zCasdoorSelfCurrentMembershipResponse,
  zCasdoorSelfEmailResponse,
  zCasdoorSelfIdentityResponse,
  zCasdoorSelfMembershipResponse,
  zCasdoorSelfNameResponse,
  zGetAccountCasdoorIdentityQuery,
  zGetAccountCasdoorIdentityResponse,
} from '@dify/contracts/api/console/account/zod.gen'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { consoleClient, consoleQuery } from '@/service/console'

const canonicalId = (value: string | null) =>
  value === null || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
const identity = zCasdoorSelfIdentityResponse
  .extend({
    id: zCasdoorSelfIdentityResponse.shape.id.refine(canonicalId),
    namespace_id: zCasdoorSelfIdentityResponse.shape.namespace_id.refine(canonicalId),
    email: zCasdoorSelfEmailResponse.strict(),
    name: zCasdoorSelfNameResponse.strict(),
  })
  .strict()
const membership = zCasdoorSelfMembershipResponse
  .extend({
    id: zCasdoorSelfMembershipResponse.shape.id.refine(canonicalId),
    identity_id: zCasdoorSelfMembershipResponse.shape.identity_id.refine(canonicalId),
    namespace_id: zCasdoorSelfMembershipResponse.shape.namespace_id.refine(canonicalId),
    workspace_id: zCasdoorSelfMembershipResponse.shape.workspace_id.refine(canonicalId),
  })
  .strict()
const currentMembership = zCasdoorSelfCurrentMembershipResponse
  .extend({
    id: zCasdoorSelfCurrentMembershipResponse.shape.id.refine(canonicalId),
    workspace_id: zCasdoorSelfCurrentMembershipResponse.shape.workspace_id.refine(canonicalId),
  })
  .strict()
const responseSchema = zGetAccountCasdoorIdentityResponse
  .extend({
    actions: zCasdoorSelfActionsResponse.strict(),
    identities: identity.array(),
    memberships: membership.array(),
    current_memberships: currentMembership.array(),
    identity_next: zGetAccountCasdoorIdentityResponse.shape.identity_next.refine(canonicalId),
    membership_next: zGetAccountCasdoorIdentityResponse.shape.membership_next.refine(canonicalId),
    current_membership_next:
      zGetAccountCasdoorIdentityResponse.shape.current_membership_next.refine(canonicalId),
  })
  .strict()
const querySchema = zGetAccountCasdoorIdentityQuery
  .extend({
    identity_after: zGetAccountCasdoorIdentityQuery.shape.identity_after.refine(
      (value) => value === undefined || canonicalId(value),
    ),
    membership_after: zGetAccountCasdoorIdentityQuery.shape.membership_after.refine(
      (value) => value === undefined || canonicalId(value),
    ),
    current_membership_after: zGetAccountCasdoorIdentityQuery.shape.current_membership_after.refine(
      (value) => value === undefined || canonicalId(value),
    ),
  })
  .strict()

export function casdoorIdentityQueryOptions(
  client: QueryClient,
  query?: GetAccountCasdoorIdentityData['query'],
) {
  const parsed = querySchema.safeParse(query === undefined ? {} : query)
  if (!parsed.success) throw new Error('casdoor_self_invalid_query')
  const input = { query: parsed.data }
  const profileKey = userProfileQueryOptions().queryKey
  const readAnchor = () => {
    const id = client.getQueryData(profileKey)?.profile?.id
    return typeof id === 'string' && id.trim().length > 0 ? id : undefined
  }
  const anchor = readAnchor()
  const checkAnchor = () => {
    if (!anchor || readAnchor() !== anchor) throw new Error('casdoor_self_context_changed')
  }
  // This tracks the existing profile cache, not unseen cookie changes in another tab.
  return consoleQuery.account.casdoorIdentity.get.queryOptions({
    input,
    queryKey: [
      ...consoleQuery.account.casdoorIdentity.get.queryKey({ input }),
      { profileAnchor: anchor ?? null },
    ],
    enabled: anchor !== undefined,
    queryFn: async ({ signal }): Promise<GetAccountCasdoorIdentityResponse> => {
      checkAnchor()
      let data: unknown
      try {
        data = await consoleClient.account.casdoorIdentity.get(input, {
          signal,
          context: { silent: true },
        })
      } catch {
        throw new Error('casdoor_self_unavailable')
      }
      const decoded = responseSchema.safeParse(data)
      if (!decoded.success) throw new Error('casdoor_self_invalid_response')
      checkAnchor()
      return decoded.data
    },
  })
}
