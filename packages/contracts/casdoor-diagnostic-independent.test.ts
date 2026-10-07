import { describe, expect, it } from 'vite-plus/test'
import {
  zCasdoorDraftDiagnosticPreviewResponse,
  zCasdoorTestLoginResponse,
} from './generated/api/console/system-manage-extend/zod.gen'

const revisionId = '11111111-1111-4111-8111-111111111111'
const namespaceId = '22222222-2222-4222-8222-222222222222'
const correlationId = '33333333-3333-4333-8333-333333333333'
const workspaceId = '44444444-4444-4444-8444-444444444444'
const handoffPath = `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`

const preview = {
  correlation_id: correlationId,
  effective_role_count: 2,
  namespace_id: namespaceId,
  revision_id: revisionId,
  stages: [{ stage: 'protocol', status: 'passed' }],
  targets: [{ reason: 'role_mapping', target_role: 'editor', workspace_id: workspaceId }],
}

describe('independent generated diagnostic DTO contract', () => {
  it('accepts only the declared started or safe blocked login response values', () => {
    expect(
      zCasdoorTestLoginResponse.parse({
        status: 'started',
        reason: null,
        handoff: { handoff_path: handoffPath },
      }),
    ).toEqual({ status: 'started', reason: null, handoff: { handoff_path: handoffPath } })
    expect(
      zCasdoorTestLoginResponse.parse({
        status: 'blocked',
        reason: 'deployment_proof_missing',
        handoff: null,
      }),
    ).toMatchObject({ status: 'blocked', reason: 'deployment_proof_missing', handoff: null })
    expect(
      zCasdoorTestLoginResponse.safeParse({
        status: 'blocked',
        reason: 'provider returned secret contents',
      }).success,
    ).toBe(false)
    expect(zCasdoorTestLoginResponse.safeParse({ status: 'pending' }).success).toBe(false)
  })

  it('requires the bounded read-only preview fields and rejects unsafe role or size values', () => {
    expect(zCasdoorDraftDiagnosticPreviewResponse.parse(preview)).toEqual(preview)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({
        ...preview,
        targets: [{ ...preview.targets[0], target_role: 'owner' }],
      }).success,
    ).toBe(false)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({ ...preview, effective_role_count: 2001 })
        .success,
    ).toBe(false)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({
        ...preview,
        stages: Array.from({ length: 8 }, () => preview.stages[0]),
      }).success,
    ).toBe(false)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({
        ...preview,
        targets: Array.from({ length: 101 }, () => preview.targets[0]),
      }).success,
    ).toBe(false)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({ ...preview, correlation_id: 'invalid' })
        .success,
    ).toBe(false)
  })
})
