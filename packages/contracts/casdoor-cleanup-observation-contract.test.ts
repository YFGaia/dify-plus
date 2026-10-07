import type { CasdoorSelfIdentityResponse } from './generated/api/console/account/types.gen'
import { describe, expect, it } from 'vite-plus/test'
import { zCasdoorSelfIdentityResponse } from './generated/api/console/account/zod.gen'

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

describe('official closed cleanup observation', () => {
  it('accepts the failed cleanup diagnostic through the generated account owner', () => {
    const value: CasdoorSelfIdentityResponse = {
      ...identity(),
      avatar_status: 'failed_storage_cleaned',
      avatar_last_reason: 'attachment_lost',
      avatar_current_local_differs_from_last_applied: null,
    }
    expect(zCasdoorSelfIdentityResponse.parse(value)).toEqual(value)
  })
  it.each(['cleaned', 'cleanup_complete', 'cleanup_synced', 'retry_ready'])(
    'rejects unregistered or authority-bearing status %s',
    (avatar_status) => {
      expect(zCasdoorSelfIdentityResponse.safeParse({ ...identity(), avatar_status }).success).toBe(
        false,
      )
    },
  )
})
