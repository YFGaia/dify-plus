import { describe, expect, it } from 'vite-plus/test'
import { zCasdoorLogoutResponse } from './generated/api/console/auth/zod.gen'
import { zPostLogoutResponse } from './generated/api/console/logout/zod.gen'
import { contractLoaders } from './generated/api/console/orpc.gen'
import { zCasdoorRpLogoutDiagnosticResponse } from './generated/api/console/system-manage-extend/zod.gen'

const id = '11111111-1111-4111-8111-111111111111'

describe('generated optional RP logout caller contracts', () => {
  it('mounts only anonymous retry as JSON, keeping token-bearing browser redirects out of RPC', async () => {
    const { auth } = await contractLoaders.auth()
    expect(Object.keys(auth.casdoor.logout)).toEqual(['retry'])
    const route = auth.casdoor.logout.retry.post['~orpc']
    expect(route.route).toMatchObject({ method: 'POST', path: '/auth/casdoor/logout/retry' })
    expect(route.inputSchema).toBeUndefined()
    expect(route.outputSchema).toBe(zCasdoorLogoutResponse)
  })

  it('keeps original local success compatible and exposes only the optional opaque continuation', () => {
    expect(zPostLogoutResponse.strict().parse({ result: 'success' })).toEqual({ result: 'success' })
    expect(
      zPostLogoutResponse
        .strict()
        .parse({ result: 'success', casdoor_logout: { status: 'local_only' } }),
    ).toMatchObject({ result: 'success' })
    expect(
      zPostLogoutResponse.strict().safeParse({ result: 'success', id_token: 'private' }).success,
    ).toBe(false)
    expect(zPostLogoutResponse.strict().safeParse({ result: 'failed' }).success).toBe(false)
  })

  it('projects real observation freshness and exact draft IDs without provider artifacts', () => {
    expect(
      zCasdoorRpLogoutDiagnosticResponse.strict().parse({
        revision_id: id,
        namespace_id: id,
        status: 'passed',
        profile_available: true,
        checked_at: '2026-10-05T01:00:00Z',
        expires_at: '2026-10-05T01:05:00Z',
      }),
    ).toMatchObject({ status: 'passed' })
    for (const extra of [
      { id_token: 'private' },
      { endpoint: 'https://op.example/logout' },
      { proof_fingerprint: 'private' },
    ]) {
      expect(
        zCasdoorRpLogoutDiagnosticResponse
          .strict()
          .safeParse({ revision_id: id, namespace_id: id, ...extra }).success,
      ).toBe(false)
    }
    expect(
      zCasdoorRpLogoutDiagnosticResponse.safeParse({
        revision_id: id,
        namespace_id: id,
        expires_at: 'bad-date',
      }).success,
    ).toBe(false)
  })
})
