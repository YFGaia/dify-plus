import { describe, expect, it } from 'vite-plus/test'
import { contractLoaders } from './generated/api/console/orpc.gen'
import {
  zCasdoorAvatarRetryResponse,
  zCasdoorAvatarRetryTargetsResponse,
  zCasdoorNamespaceResetMutationPayload,
  zCasdoorNamespaceResetReviewResponse,
  zCasdoorResetNamespacePayload,
  zGetSystemManageExtendIntegrationCasdoorSyncRetryTargetsQuery,
} from './generated/api/console/system-manage-extend/zod.gen'

const id = '11111111-1111-4111-8111-111111111111'
describe('official namespace reset and avatar retry contracts', () => {
  it('registers the four actual management routes', async () => {
    const { systemManageExtend } = await contractLoaders.systemManageExtend()
    const api = systemManageExtend.integration.casdoor
    const root = '/system-manage-extend/integration/casdoor'
    for (const [operation, method, suffix] of [
      [api.resetNamespace.review.post, 'POST', '/reset-namespace/review'],
      [api.resetNamespace.post, 'POST', '/reset-namespace'],
      [api.sync.retryTargets.get, 'GET', '/sync/retry-targets'],
      [api.sync.retry.post, 'POST', '/sync/retry'],
    ] as const)
      expect(operation['~orpc'].route).toMatchObject({
        method,
        path: `${root}${suffix}`,
        inputStructure: 'detailed',
      })
  })
  it('bounds a format-only single-use review without credential or login claims', () => {
    const review = {
      review_id: 'R'.repeat(43),
      namespace_id: id,
      etag: 1,
      expires_in: 60,
      credential_check: 'format_only',
    }
    expect(
      zCasdoorResetNamespacePayload.parse({
        namespace_id: id,
        etag: 1,
        confirm_management_review: true,
      }).confirm_management_review,
    ).toBe(true)
    // The generated input is boolean; the server owns consent enforcement.
    expect(
      zCasdoorResetNamespacePayload.parse({
        namespace_id: id,
        etag: 1,
        confirm_management_review: false,
      }).confirm_management_review,
    ).toBe(false)
    expect(zCasdoorNamespaceResetReviewResponse.parse(review)).toEqual(review)
    for (const invalid of [
      { expires_in: 61 },
      { credential_check: 'login_verified' },
      { review_id: 'short' },
      { etag: -1 },
    ])
      expect(
        zCasdoorNamespaceResetReviewResponse.safeParse({ ...review, ...invalid }).success,
      ).toBe(false)
    expect(
      zCasdoorNamespaceResetMutationPayload.parse({ review_id: review.review_id, etag: 1 }),
    ).toEqual({ review_id: review.review_id, etag: 1 })
    expect(zCasdoorNamespaceResetReviewResponse.shape).not.toHaveProperty('secret')
  })
  it('keeps an empty filtered page with a forward cursor and strict pending-only mutation semantics', () => {
    expect(zGetSystemManageExtendIntegrationCasdoorSyncRetryTargetsQuery.parse({})).toEqual({
      limit: 20,
    })
    for (const bad of [{ after: 'bad' }, { limit: 0 }, { limit: 101 }, { limit: '20' }])
      expect(
        zGetSystemManageExtendIntegrationCasdoorSyncRetryTargetsQuery.safeParse(bad).success,
      ).toBe(false)
    expect(
      zCasdoorAvatarRetryTargetsResponse.parse({ targets: [], next_after: id, has_more: true }),
    ).toEqual({ targets: [], next_after: id, has_more: true })
    const target = {
      account_id: id,
      identity_id: id,
      intent_id: id,
      reason: 'image_rejected',
      retry_eligible: true,
    }
    expect(
      zCasdoorAvatarRetryTargetsResponse.safeParse({
        targets: [target],
        next_after: null,
        has_more: false,
      }).success,
    ).toBe(true)
    expect(
      zCasdoorAvatarRetryTargetsResponse.safeParse({
        targets: [{ ...target, reason: 'attached' }],
        next_after: null,
        has_more: false,
      }).success,
    ).toBe(false)
    expect(zCasdoorAvatarRetryResponse.parse({ status: 'pending', intent_id: id })).toEqual({
      status: 'pending',
      intent_id: id,
    })
    for (const status of ['completed', 'failed', 'eligible'])
      expect(zCasdoorAvatarRetryResponse.safeParse({ status, intent_id: id }).success).toBe(false)
  })
})
