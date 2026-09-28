// @vitest-environment node

import { QueryClient } from '@tanstack/react-query'
import { createSystemFeaturesFixture } from '@/test/console/system-features'

const mocks = vi.hoisted(() => ({
  connection: vi.fn(async () => undefined),
  getQueryClient: vi.fn(),
  getSystemFeatures: vi.fn(),
  queryKey: [['console', 'systemFeatures', 'get'], { type: 'query' }] as const,
}))

vi.mock('server-only', () => ({}))

vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react')>()

  return {
    ...actual,
    cache: (factory: () => unknown) => {
      let initialized = false
      let value: unknown

      return () => {
        if (!initialized) {
          value = factory()
          initialized = true
        }

        return value
      }
    },
  }
})

vi.mock('@/app/get-query-client', () => ({
  getQueryClient: mocks.getQueryClient,
}))

vi.mock('@/next/server', () => ({
  connection: mocks.connection,
}))

vi.mock('@/service/console', () => ({
  consoleQuery: {
    systemFeatures: {
      get: {
        queryOptions: (options?: { staleTime?: 'static' }) => ({
          queryKey: mocks.queryKey,
          queryFn: mocks.getSystemFeatures,
          retry: false,
          ...options,
        }),
      },
    },
  },
}))

describe('System Features server requests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.resetModules()
    mocks.getQueryClient.mockImplementation(
      () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    )
    mocks.connection.mockResolvedValue(undefined)
    mocks.getSystemFeatures.mockResolvedValue(
      createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }),
    )
  })

  it('reuses a successful optional lookup for the rest of the request', async () => {
    const { dehydrateSystemFeatures, getOptionalSystemFeatures } = await import('../server')

    await expect(getOptionalSystemFeatures()).resolves.toEqual(
      createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }),
    )
    await expect(getOptionalSystemFeatures()).resolves.toEqual(
      createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }),
    )

    expect(mocks.getSystemFeatures).toHaveBeenCalledOnce()
    expect(mocks.getQueryClient).toHaveBeenCalledOnce()
    expect(dehydrateSystemFeatures().queries).toEqual([
      expect.objectContaining({
        queryKey: mocks.queryKey,
        state: expect.objectContaining({
          data: createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }),
          status: 'success',
        }),
      }),
    ])
  })

  it('keeps optional failures soft and lets required consumers retry', async () => {
    mocks.getSystemFeatures
      .mockRejectedValueOnce(new Error('System Features unavailable'))
      .mockResolvedValueOnce(createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }))
    const { getOptionalSystemFeatures, getSystemFeatures } = await import('../server')

    await expect(getOptionalSystemFeatures()).resolves.toBeUndefined()
    await expect(getOptionalSystemFeatures()).resolves.toBeUndefined()
    await expect(getSystemFeatures()).resolves.toEqual(
      createSystemFeaturesFixture({ deployment_edition: 'CLOUD' }),
    )

    expect(mocks.getSystemFeatures).toHaveBeenCalledTimes(2)
  })

  it('preserves required failures for hard route gates', async () => {
    const error = new Error('System Features unavailable')
    mocks.getSystemFeatures.mockRejectedValue(error)
    const { getSystemFeatures } = await import('../server')

    await expect(getSystemFeatures()).rejects.toBe(error)
  })
})

describe('System Features SSR snapshot validation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.resetModules()
    mocks.getQueryClient.mockImplementation(
      () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    )
  })

  it.each([
    { ping: true },
    { deployment_edition: 'COMMUNITY' },
    { ...createSystemFeaturesFixture(), branding: undefined },
    { ...createSystemFeaturesFixture(), license: {} },
    { ...createSystemFeaturesFixture(), enable_email_password_login: 'true' },
  ])('never hydrates invalid successful responses: %j', async (response) => {
    mocks.getSystemFeatures.mockResolvedValue(response)
    const { dehydrateSystemFeatures, getOptionalSystemFeatures, getSystemFeatures } =
      await import('../server')

    await expect(getOptionalSystemFeatures()).resolves.toBeUndefined()
    await expect(getOptionalSystemFeatures()).resolves.toBeUndefined()
    expect(mocks.getSystemFeatures).toHaveBeenCalledOnce()
    expect(dehydrateSystemFeatures().queries).toEqual([])
    await expect(getSystemFeatures()).rejects.toThrow()
    expect(mocks.getSystemFeatures).toHaveBeenCalledTimes(2)
    expect(dehydrateSystemFeatures().queries).toEqual([])
  })

  it('hydrates only public fields even if a backend mistakenly includes private data', async () => {
    const features = createSystemFeaturesFixture()
    mocks.getSystemFeatures.mockResolvedValue({
      ...features,
      ding_talk_client_id: 'fork-client',
      license: { ...features.license, seats: { size: 5 }, expired_at: '2030-01-01' },
    })
    const { dehydrateSystemFeatures, getSystemFeatures } = await import('../server')
    await expect(getSystemFeatures()).resolves.toEqual(features)
    expect(dehydrateSystemFeatures().queries[0]?.state.data).toEqual(features)
  })
})
