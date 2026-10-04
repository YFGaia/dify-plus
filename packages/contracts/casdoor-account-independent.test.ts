import type {
  CasdoorSelfIdentityStatusResponse,
  GetAccountCasdoorIdentityData,
} from './generated/api/console/account/types.gen'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vite-plus/test'
import {
  zCasdoorSelfActionsResponse,
  zCasdoorSelfCurrentMembershipResponse,
  zCasdoorSelfEmailResponse,
  zCasdoorSelfErrorResponse,
  zCasdoorSelfIdentityResponse,
  zCasdoorSelfIdentityStatusResponse,
  zCasdoorSelfMembershipResponse,
  zCasdoorSelfNameResponse,
  zGetAccountCasdoorIdentityQuery,
} from './generated/api/console/account/zod.gen'
import { contractLoaders } from './generated/api/console/orpc.gen'

const uuid = '11111111-1111-4111-8111-111111111111'

const response: CasdoorSelfIdentityStatusResponse = {
  actions: {
    adopt: false,
    link: false,
    logout: false,
    reauthenticate: false,
    release: false,
    retry: false,
    unlink: false,
  },
  binding: 'linked',
  current_membership_has_more: true,
  current_membership_next: uuid,
  current_memberships: [
    {
      id: uuid,
      join_presence: 'present',
      local_role: 'admin',
      remote_actual_state: 'unknown',
      state: 'history_present',
      workspace_id: uuid,
    },
  ],
  identities: [
    {
      activity: 'active',
      avatar_status: 'unknown',
      email: {
        current_differs: false,
        last_differs: null,
        last_status: 'same',
        verified: true,
      },
      id: uuid,
      lifecycle: 'active',
      masked_identifier: '********',
      name: {
        baseline_generation: 0,
        current_local_differs_from_last_applied: null,
        last_reason: 'created_baseline',
        last_status: 'applied',
        last_sync_at: 'not-validated-by-the-generated-schema',
        recorded_generation: 1,
      },
      namespace_id: uuid,
      organization: 'synthetic-org',
      profile_consistency: 'consistent',
      sync_generation: 1,
    },
  ],
  identity_has_more: false,
  identity_next: null,
  membership_has_more: false,
  membership_next: null,
  memberships: [
    {
      consistency: 'historical',
      id: uuid,
      identity_id: uuid,
      join_presence: 'unknown',
      local_role: null,
      namespace_id: uuid,
      recorded_finalization: 'manual_recovery',
      recorded_ownership: 'local_override',
      recorded_source: 'fallback',
      remote_actual_state: 'unknown',
      state: 'controlled_withdrawal',
      tombstone: null,
      workspace_id: uuid,
    },
  ],
}

type Operation = {
  '~orpc': {
    inputSchema?: {
      parse: (input: unknown) => unknown
      safeParse: (input: unknown) => { success: boolean }
    }
    outputSchema?: unknown
    route: { inputStructure?: string; method?: string; operationId?: string; path?: string }
  }
}

function collectOperations(value: unknown, result: Operation[] = []): Operation[] {
  if (!value || typeof value !== 'object') return result
  if ('~orpc' in value && typeof value['~orpc'] === 'object' && value['~orpc'] !== null) {
    result.push(value as Operation)
    return result
  }
  for (const child of Object.values(value)) collectOperations(child, result)
  return result
}

function rawOpenApi() {
  return JSON.parse(
    readFileSync(new URL('./openapi/console-openapi.json', import.meta.url), 'utf8'),
  ) as {
    components: { schemas: Record<string, { additionalProperties?: boolean }> }
    paths: Record<
      string,
      Record<
        string,
        {
          operationId?: string
          parameters?: Array<{
            in: string
            name: string
            required: boolean
            schema: Record<string, unknown>
          }>
          responses?: Record<
            string,
            { content?: { 'application/json'?: { schema?: { $ref?: string } } } }
          >
        }
      >
    >
  }
}

function assertAllFieldsRequired(
  schema: { safeParse: (input: unknown) => { success: boolean } },
  value: object,
) {
  for (const field of Object.keys(value)) {
    const omitted = { ...value } as Record<string, unknown>
    delete omitted[field]
    expect(schema.safeParse(omitted).success).toBe(false)
  }
}

describe('independent native generated Casdoor account contract', () => {
  it('loads the self-identity endpoint as a read-only GET with no account selector, body, HEAD, or OPTIONS', async () => {
    const { account } = await contractLoaders.account()
    const operations = collectOperations(account)
    const endpoint = operations.filter(
      ({ '~orpc': { route } }) => route.path === '/account/casdoor-identity',
    )

    expect(endpoint).toHaveLength(1)
    expect(endpoint[0]?.['~orpc'].route).toMatchObject({
      inputStructure: 'detailed',
      method: 'GET',
      operationId: 'getAccountCasdoorIdentity',
      path: '/account/casdoor-identity',
    })
    expect(endpoint[0]?.['~orpc'].outputSchema).toBe(zCasdoorSelfIdentityStatusResponse)
    expect(
      operations.some(
        ({ '~orpc': { route } }) =>
          route.path === '/account/casdoor-identity' && route.method !== 'GET',
      ),
    ).toBe(false)
    expect(account.casdoorIdentity.get['~orpc'].inputSchema?.safeParse({}).success).toBe(true)
    expect(account.casdoorIdentity.get['~orpc'].inputSchema?.parse({ body: {} })).toEqual({})

    const consumerCall: GetAccountCasdoorIdentityData = {
      query: {
        current_membership_after: uuid,
        identity_after: uuid,
        limit: 20,
        membership_after: uuid,
      },
      url: '/account/casdoor-identity',
    }
    expect(consumerCall.url).toBe('/account/casdoor-identity')
    expect(consumerCall.body).toBeUndefined()
    expect(consumerCall.path).toBeUndefined()
  })

  it('matches raw query names, UUID/integer constraints, bounds, and default while exposing three independent cursors', () => {
    const { paths } = rawOpenApi()
    const rawGet = paths['/account/casdoor-identity']?.get
    expect(Object.keys(paths['/account/casdoor-identity'] ?? {}).sort()).toEqual(['get'])
    expect(rawGet?.operationId).toBe('get_casdoor_self_identity_api')
    expect(rawGet?.parameters).toEqual([
      {
        in: 'query',
        name: 'current_membership_after',
        required: false,
        schema: { format: 'uuid', type: 'string' },
      },
      {
        in: 'query',
        name: 'identity_after',
        required: false,
        schema: { format: 'uuid', type: 'string' },
      },
      {
        in: 'query',
        name: 'limit',
        required: false,
        schema: { default: 20, maximum: 50, minimum: 1, type: 'integer' },
      },
      {
        in: 'query',
        name: 'membership_after',
        required: false,
        schema: { format: 'uuid', type: 'string' },
      },
    ])
    expect(rawGet?.responses).toMatchObject({
      '200': {
        content: {
          'application/json': {
            schema: { $ref: '#/components/schemas/CasdoorSelfIdentityStatusResponse' },
          },
        },
      },
      '400': {
        content: {
          'application/json': {
            schema: { $ref: '#/components/schemas/CasdoorSelfErrorResponse' },
          },
        },
      },
      '409': {
        content: {
          'application/json': {
            schema: { $ref: '#/components/schemas/CasdoorSelfErrorResponse' },
          },
        },
      },
      '503': {
        content: {
          'application/json': {
            schema: { $ref: '#/components/schemas/CasdoorSelfErrorResponse' },
          },
        },
      },
    })

    expect(zGetAccountCasdoorIdentityQuery.parse({})).toEqual({ limit: 20 })
    expect(zGetAccountCasdoorIdentityQuery.parse({ limit: 1 }).limit).toBe(1)
    expect(zGetAccountCasdoorIdentityQuery.parse({ limit: 50 }).limit).toBe(50)
    for (const limit of [0, 51, 1.5, '20']) {
      expect(zGetAccountCasdoorIdentityQuery.safeParse({ limit }).success).toBe(false)
    }
    expect(
      zGetAccountCasdoorIdentityQuery.parse({
        current_membership_after: uuid,
        identity_after: uuid,
        membership_after: uuid,
      }),
    ).toEqual({
      current_membership_after: uuid,
      identity_after: uuid,
      limit: 20,
      membership_after: uuid,
    })
    for (const cursor of ['current_membership_after', 'identity_after', 'membership_after']) {
      expect(zGetAccountCasdoorIdentityQuery.safeParse({ [cursor]: 'not-a-uuid' }).success).toBe(
        false,
      )
    }

    // Zod's UUID parser accepts uppercase hex; the backend canonical-UUID comparison gap is tracked separately.
    const uppercaseUuid = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
    expect(
      zGetAccountCasdoorIdentityQuery.safeParse({ identity_after: uppercaseUuid }).success,
    ).toBe(true)
    expect(zGetAccountCasdoorIdentityQuery.parse({ selector: 'another-account' })).toEqual({
      limit: 20,
    })
  })

  it('parses a fully populated response while keeping observations passive and all three pages independent', () => {
    expect(zCasdoorSelfIdentityStatusResponse.parse(response)).toEqual(response)
    expect(response.actions).toEqual({
      adopt: false,
      link: false,
      logout: false,
      reauthenticate: false,
      release: false,
      retry: false,
      unlink: false,
    })
    for (const field of [
      'current_memberships',
      'current_membership_has_more',
      'current_membership_next',
      'identities',
      'identity_has_more',
      'identity_next',
      'memberships',
      'membership_has_more',
      'membership_next',
    ]) {
      const omitted = { ...response } as Record<string, unknown>
      delete omitted[field]
      expect(zCasdoorSelfIdentityStatusResponse.safeParse(omitted).success).toBe(false)
    }
    assertAllFieldsRequired(zCasdoorSelfIdentityStatusResponse, response)
    expect(zCasdoorSelfActionsResponse.safeParse({ ...response.actions, link: true }).success).toBe(
      false,
    )
    expect(
      zCasdoorSelfIdentityResponse.safeParse({
        ...response.identities[0],
        masked_identifier: 'alice@example.test',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorSelfIdentityResponse.safeParse({
        ...response.identities[0],
        avatar_status: 'available',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorSelfMembershipResponse.safeParse({
        ...response.memberships[0],
        remote_actual_state: 'present',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorSelfCurrentMembershipResponse.safeParse({
        ...response.current_memberships[0],
        remote_actual_state: 'absent',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorSelfIdentityResponse.safeParse({
        ...response.identities[0],
        organization: 'x'.repeat(256),
      }).success,
    ).toBe(false)
  })

  it('accepts explicit nulls for every nullable response field without making those fields optional', () => {
    expect(
      zCasdoorSelfIdentityStatusResponse.parse({
        ...response,
        current_membership_next: null,
        identity_next: null,
        membership_next: null,
      }),
    ).toMatchObject({
      current_membership_next: null,
      identity_next: null,
      membership_next: null,
    })
    expect(
      zCasdoorSelfCurrentMembershipResponse.parse({
        ...response.current_memberships[0],
        id: null,
        local_role: null,
        workspace_id: null,
      }),
    ).toMatchObject({ id: null, local_role: null, workspace_id: null })
    expect(
      zCasdoorSelfMembershipResponse.parse({
        ...response.memberships[0],
        id: null,
        identity_id: null,
        local_role: null,
        namespace_id: null,
        recorded_finalization: null,
        recorded_ownership: null,
        recorded_source: null,
        tombstone: null,
        workspace_id: null,
      }),
    ).toMatchObject({
      id: null,
      identity_id: null,
      local_role: null,
      namespace_id: null,
      recorded_finalization: null,
      recorded_ownership: null,
      recorded_source: null,
      tombstone: null,
      workspace_id: null,
    })
    expect(
      zCasdoorSelfIdentityResponse.parse({
        ...response.identities[0],
        id: null,
        namespace_id: null,
        organization: null,
        sync_generation: null,
      }),
    ).toMatchObject({ id: null, namespace_id: null, organization: null, sync_generation: null })
    expect(
      zCasdoorSelfEmailResponse.parse({
        current_differs: null,
        last_differs: null,
        last_status: null,
        verified: null,
      }),
    ).toEqual({ current_differs: null, last_differs: null, last_status: null, verified: null })
    expect(
      zCasdoorSelfNameResponse.parse({
        baseline_generation: null,
        current_local_differs_from_last_applied: null,
        last_reason: null,
        last_status: null,
        last_sync_at: null,
        recorded_generation: null,
      }),
    ).toEqual({
      baseline_generation: null,
      current_local_differs_from_last_applied: null,
      last_reason: null,
      last_status: null,
      last_sync_at: null,
      recorded_generation: null,
    })
  })

  it('accepts every documented nullable enum value and rejects unknown enum values', () => {
    const checks = [
      [zCasdoorSelfIdentityStatusResponse.shape.binding, ['linked', 'unlinked']],
      [
        zCasdoorSelfErrorResponse.shape.code,
        [
          'casdoor_self_invalid_query',
          'casdoor_self_read_conflict',
          'casdoor_self_request_rejected',
          'casdoor_self_unavailable',
        ],
      ],
      [zCasdoorSelfCurrentMembershipResponse.shape.join_presence, ['absent', 'present', 'unknown']],
      [
        zCasdoorSelfCurrentMembershipResponse.shape.local_role,
        ['admin', 'dataset_operator', 'editor', 'normal', 'owner', null],
      ],
      [
        zCasdoorSelfCurrentMembershipResponse.shape.state,
        ['history_present', 'unknown', 'unmanaged'],
      ],
      [zCasdoorSelfMembershipResponse.shape.consistency, ['historical', 'stale', 'unknown']],
      [zCasdoorSelfMembershipResponse.shape.join_presence, ['absent', 'present', 'unknown']],
      [
        zCasdoorSelfMembershipResponse.shape.local_role,
        ['admin', 'dataset_operator', 'editor', 'normal', 'owner', null],
      ],
      [
        zCasdoorSelfMembershipResponse.shape.recorded_finalization,
        ['finalized', 'manual_recovery', 'pending', null],
      ],
      [
        zCasdoorSelfMembershipResponse.shape.recorded_ownership,
        ['local_override', 'managed', 'released', null],
      ],
      [
        zCasdoorSelfMembershipResponse.shape.recorded_source,
        ['adopt', 'fallback', 'mapping', null],
      ],
      [
        zCasdoorSelfMembershipResponse.shape.state,
        [
          'absent_unknown',
          'controlled_withdrawal',
          'historical',
          'local_override',
          'recorded_managed',
          'tombstone',
          'unknown',
          'unmanaged',
        ],
      ],
      [zCasdoorSelfIdentityResponse.shape.activity, ['active', 'inactive', 'unknown']],
      [zCasdoorSelfIdentityResponse.shape.lifecycle, ['active', 'archived', 'fencing', 'unknown']],
      [
        zCasdoorSelfIdentityResponse.shape.profile_consistency,
        ['consistent', 'historical', 'unknown'],
      ],
      [
        zCasdoorSelfEmailResponse.shape.last_status,
        ['different', 'invalid', 'same', 'unavailable', null],
      ],
      [
        zCasdoorSelfNameResponse.shape.last_reason,
        [
          'ambiguous_profile_attempt',
          'created_baseline',
          'disabled',
          'empty_remote_name',
          'filled_empty',
          'local_name_present',
          'local_override',
          'managed_update',
          'same_name',
          'stale_profile_attempt',
          'unowned_local_name',
          null,
        ],
      ],
      [
        zCasdoorSelfNameResponse.shape.last_status,
        ['applied', 'disabled', 'local_override', 'skipped', 'unchanged', null],
      ],
    ] as const

    for (const [schema, values] of checks) {
      for (const value of values) expect(schema.safeParse(value).success).toBe(true)
      expect(schema.safeParse('not-a-contract-enum').success).toBe(false)
    }
    for (const code of [
      'casdoor_self_invalid_query',
      'casdoor_self_read_conflict',
      'casdoor_self_request_rejected',
      'casdoor_self_unavailable',
    ]) {
      expect(zCasdoorSelfErrorResponse.parse({ code })).toEqual({ code })
    }
  })

  it('records raw closed objects versus generated Zod stripping and checks required fields without asserting strict Zod behavior', () => {
    const { components } = rawOpenApi()
    for (const name of [
      'CasdoorSelfIdentityStatusResponse',
      'CasdoorSelfActionsResponse',
      'CasdoorSelfCurrentMembershipResponse',
      'CasdoorSelfMembershipResponse',
      'CasdoorSelfIdentityResponse',
      'CasdoorSelfEmailResponse',
      'CasdoorSelfNameResponse',
      'CasdoorSelfErrorResponse',
    ]) {
      expect(components.schemas[name]?.additionalProperties).toBe(false)
    }

    const withExtra = { ...response, unexpected: 'stripped by generated Zod' }
    const parsed = zCasdoorSelfIdentityStatusResponse.parse(withExtra)
    expect(parsed).toEqual(response)
    expect(parsed).not.toHaveProperty('unexpected')
    expect(
      zCasdoorSelfErrorResponse.parse({ code: 'casdoor_self_unavailable', extra: true }),
    ).toEqual({ code: 'casdoor_self_unavailable' })

    assertAllFieldsRequired(zCasdoorSelfActionsResponse, response.actions)
    assertAllFieldsRequired(zCasdoorSelfCurrentMembershipResponse, response.current_memberships[0]!)
    assertAllFieldsRequired(zCasdoorSelfMembershipResponse, response.memberships[0]!)
    assertAllFieldsRequired(zCasdoorSelfIdentityResponse, response.identities[0]!)
    assertAllFieldsRequired(zCasdoorSelfEmailResponse, response.identities[0]!.email)
    assertAllFieldsRequired(zCasdoorSelfNameResponse, response.identities[0]!.name)
    expect(zCasdoorSelfErrorResponse.safeParse({}).success).toBe(false)
    expect(
      zCasdoorSelfIdentityStatusResponse.safeParse({ ...response, binding: 'pending' }).success,
    ).toBe(false)
  })
})
