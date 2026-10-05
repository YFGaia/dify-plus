import { describe, expect, it } from 'vite-plus/test'
import { zCasdoorSessionResponse } from './generated/api/console/auth/zod.gen'
import { contractLoaders } from './generated/api/console/orpc.gen'

describe('generated private Casdoor session contract', () => {
  it('mounts the native read-only session GET without public source inputs', async () => {
    const { auth } = await contractLoaders.auth()
    const session = auth.casdoor.session.get['~orpc']
    expect(session.route).toMatchObject({
      inputStructure: 'detailed',
      method: 'GET',
      operationId: 'getAuthCasdoorSession',
      path: '/auth/casdoor/session',
    })
    expect(session.inputSchema).toBeUndefined()
    expect(session.outputSchema).toBe(zCasdoorSessionResponse)
  })

  it('exposes only the minimal source summary and optional UTC expiry', () => {
    expect(
      zCasdoorSessionResponse.strict().parse({ source: 'local_only', verified: false }),
    ).toEqual({ source: 'local_only', verified: false, rp_logout_available: false })
    expect(
      zCasdoorSessionResponse.strict().parse({
        source: 'casdoor',
        verified: true,
        rp_logout_available: false,
        expires_at: '2026-10-05T00:00:00Z',
      }),
    ).toMatchObject({ source: 'casdoor', verified: true })
    expect(
      zCasdoorSessionResponse.safeParse({ source: 'public_flag', verified: true }).success,
    ).toBe(false)
    expect(
      zCasdoorSessionResponse.safeParse({
        source: 'casdoor',
        verified: true,
        expires_at: 'not-a-date',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorSessionResponse
        .strict()
        .safeParse({ source: 'local_only', verified: false, id_token: 'provider-artifact' })
        .success,
    ).toBe(false)
  })
})
