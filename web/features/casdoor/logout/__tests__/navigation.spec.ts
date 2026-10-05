import { logoutHandoffDestination } from '../navigation'

const configuration = vi.hoisted(() => ({ API_PREFIX: 'https://console.example/console/api' }))
vi.mock('@/config', () => configuration)

const opaque = 'A'.repeat(43)
const handoff = `/console/api/auth/casdoor/logout/${opaque}`

describe('RP logout navigation', () => {
  beforeEach(() => {
    configuration.API_PREFIX = 'https://console.example/console/api'
  })

  it('pins the opaque handoff to the configured console API origin', () => {
    expect(logoutHandoffDestination({ handoff_path: handoff })).toBe(
      `https://console.example${handoff}`,
    )
  })

  it.each(['localhost', '127.0.0.1', '[::1]'])(
    'supports the console loopback HTTP development origin %s',
    (host) => {
      configuration.API_PREFIX = `http://${host}:5001/console/api`
      expect(logoutHandoffDestination({ handoff_path: handoff })).toBe(
        `http://${host}:5001${handoff}`,
      )
    },
  )

  it.each([
    'https://provider.example/logout',
    '//provider.example/logout',
    `${handoff}?id_token_hint=private`,
    `${handoff}#state`,
    `/console/api/auth/casdoor/logout/${'A'.repeat(42)}B`,
    `/console/api/auth/casdoor/logout/retry`,
    `/console/api/auth/casdoor/diagnostic/${opaque}`,
    `/console/api/auth/casdoor/logout/%41${'A'.repeat(42)}`,
    `${handoff}/..`,
    `${handoff}\\`,
  ])('rejects a substituted destination %s', (handoff_path) => {
    expect(() => logoutHandoffDestination({ handoff_path })).toThrow()
  })

  it.each([
    'http://console.example/console/api',
    'https://user:password@console.example/console/api',
    'https://console.example/console/api?query=1',
    'https://console.example/console/api#fragment',
    'https://console.example/another-api',
  ])('rejects an untrusted API origin %s', (api) => {
    configuration.API_PREFIX = api
    expect(() => logoutHandoffDestination({ handoff_path: handoff })).toThrow()
  })

  it('rejects payloads that could carry provider credentials alongside the path', () => {
    expect(() =>
      logoutHandoffDestination({ handoff_path: handoff, id_token_hint: 'private' }),
    ).toThrow()
  })
})
