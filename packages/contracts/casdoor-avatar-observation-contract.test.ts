import type { CasdoorSelfIdentityResponse } from './generated/api/console/account/types.gen'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vite-plus/test'
import { zCasdoorSelfIdentityResponse } from './generated/api/console/account/zod.gen'
import { contractLoaders } from './generated/api/console/orpc.gen'

const recordedAt = '2026-10-05T03:00:00.000000+00:00'
const identity = (): CasdoorSelfIdentityResponse => ({
  id: '11111111-1111-4111-8111-111111111111',
  namespace_id: '22222222-2222-4222-8222-222222222222',
  organization: 'Synthetic organization',
  masked_identifier: '********',
  activity: 'active',
  lifecycle: 'active',
  sync_generation: 1,
  profile_consistency: 'consistent',
  name: {
    last_status: null,
    last_reason: null,
    last_sync_at: null,
    recorded_generation: null,
    baseline_generation: null,
    current_local_differs_from_last_applied: null,
  },
  email: { current_differs: null, verified: null, last_status: null, last_differs: null },
  avatar_status: 'local_attachment_recorded',
  avatar_recorded_at: recordedAt,
  avatar_last_reason: null,
  avatar_recorded_generation: 1,
  avatar_consistency: 'current',
  avatar_current_local_differs_from_last_applied: false,
})

describe('official generated self avatar observation contract', () => {
  it('loads the existing native account GET and documents operation time without physical synchronization proof', async () => {
    const { account } = await contractLoaders.account()
    expect(account.casdoorIdentity.get['~orpc'].route).toMatchObject({
      method: 'GET',
      path: '/account/casdoor-identity',
    })
    const openapi = JSON.parse(
      readFileSync(new URL('./openapi/console-openapi.json', import.meta.url), 'utf8'),
    )
    const schema = openapi.components.schemas.CasdoorSelfIdentityResponse
    expect(schema.properties.avatar_recorded_at.description).toContain(
      'does not prove physical storage',
    )
    expect(schema.properties.avatar_status.enum).toContain('local_attachment_recorded')
    expect(schema.properties.avatar_status.enum).not.toContain('synced')
    expect(schema.required).toEqual(
      expect.arrayContaining([
        'avatar_status',
        'avatar_recorded_at',
        'avatar_last_reason',
        'avatar_recorded_generation',
        'avatar_consistency',
        'avatar_current_local_differs_from_last_applied',
      ]),
    )
    expect(zCasdoorSelfIdentityResponse.parse(identity())).toEqual(identity())
  })
  it.each([
    'off',
    'no_record',
    'pending',
    'source_expired',
    'in_flight',
    'unknown',
    'failed_before_storage',
    'local_attachment_recorded',
    'local_override',
    'historical',
  ])(
    'accepts documented operation status %s without introducing a synced state',
    (avatar_status) => {
      expect(zCasdoorSelfIdentityResponse.safeParse({ ...identity(), avatar_status }).success).toBe(
        true,
      )
    },
  )
  it.each([
    ['avatar_status', 'synced'],
    ['avatar_last_reason', 'raw_supplier_error'],
    ['avatar_consistency', 'online'],
    ['avatar_recorded_generation', -1],
    ['avatar_recorded_at', 'x'.repeat(41)],
    ['avatar_current_local_differs_from_last_applied', 1],
  ])('rejects invalid public field %s', (key, value) => {
    expect(
      zCasdoorSelfIdentityResponse.safeParse({ ...identity(), [key as string]: value }).success,
    ).toBe(false)
  })
  it('keeps nullable operation facts required rather than defaulting missing evidence', () => {
    const value = identity()
    Object.assign(value, {
      avatar_status: 'unknown',
      avatar_recorded_at: null,
      avatar_last_reason: null,
      avatar_recorded_generation: null,
      avatar_consistency: 'unknown',
      avatar_current_local_differs_from_last_applied: null,
    })
    expect(zCasdoorSelfIdentityResponse.parse(value)).toEqual(value)
    const { avatar_recorded_at, ...missing } = value
    expect(avatar_recorded_at).toBeNull()
    expect(zCasdoorSelfIdentityResponse.safeParse(missing).success).toBe(false)
  })
})
