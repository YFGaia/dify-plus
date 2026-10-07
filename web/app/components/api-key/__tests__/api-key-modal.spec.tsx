import type { ApiKeyList as AppApiKeyList } from '@dify/contracts/api/console/apps/types.gen'
import type { ApiKeyList as DatasetApiKeyList } from '@dify/contracts/api/console/datasets/types.gen'
import type { EnvironmentApiKey } from '@dify/contracts/enterprise-app-deploy/types.gen'
import type { ComponentProps } from 'react'
import { QueryClientProvider, skipToken } from '@tanstack/react-query'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach } from 'vite-plus/test'
import { render } from '@/test/console/render'
import { createTestQueryClient } from '@/test/query-client'
import { ApiKeyModal } from '../api-key-modal'

const apiMocks = vi.hoisted(() => ({
  appKeys: [] as AppApiKeyList['data'],
  datasetKeys: [] as DatasetApiKeyList['data'],
  environmentKeys: [] as EnvironmentApiKey[],
  listApp: vi.fn(),
  createApp: vi.fn(),
  updateApp: vi.fn(),
  deleteApp: vi.fn(),
  listDataset: vi.fn(),
  createDataset: vi.fn(),
  deleteDataset: vi.fn(),
  listEnvironment: vi.fn(),
  createEnvironment: vi.fn(),
  deleteEnvironment: vi.fn(),
}))

vi.mock('@/service/console', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/service/console')>()
  const appApiKeys = actual.consoleQuery.apps.byResourceId.apiKeys
  return {
    consoleQuery: {
      apps: {
        byResourceId: {
          apiKeys: {
            get: {
              queryKey: appApiKeys.get.queryKey,
              queryOptions: ({
                input,
              }: {
                input: { params: { resource_id: string } } | typeof skipToken
              }) => ({
                queryKey: appApiKeys.get.queryKey({ input }),
                queryFn:
                  input === skipToken
                    ? skipToken
                    : () => {
                        apiMocks.listApp(input)
                        return Promise.resolve({ data: apiMocks.appKeys })
                      },
              }),
            },
            post: {
              mutationOptions: () => ({
                ...appApiKeys.post.mutationOptions(),
                mutationFn: (variables: unknown) => apiMocks.createApp(variables),
              }),
            },
            put: {
              mutationOptions: (options: object) => ({
                ...options,
                mutationFn: (variables: unknown) => apiMocks.updateApp(variables),
              }),
            },
            byApiKeyId: {
              delete: {
                mutationOptions: () => ({
                  mutationFn: (variables: unknown) => apiMocks.deleteApp(variables),
                }),
              },
            },
          },
        },
      },
      datasets: {
        apiKeys: {
          get: {
            queryOptions: () => ({
              queryKey: ['datasets', 'api-keys'],
              queryFn: () => {
                apiMocks.listDataset()
                return Promise.resolve({ data: apiMocks.datasetKeys })
              },
            }),
          },
          post: {
            mutationOptions: () => ({
              mutationFn: (variables: unknown) => apiMocks.createDataset(variables),
            }),
          },
          byApiKeyId: {
            delete: {
              mutationOptions: () => ({
                mutationFn: (variables: unknown) => apiMocks.deleteDataset(variables),
              }),
            },
          },
        },
      },
      enterprise: {
        appDeploy: {
          accessService: {
            listEnvironmentApiKeys: {
              queryOptions: ({ input }: { input: unknown }) => ({
                queryKey: ['environment', 'api-keys', input],
                queryFn:
                  input === skipToken
                    ? skipToken
                    : () => {
                        apiMocks.listEnvironment(input)
                        return Promise.resolve({ data: apiMocks.environmentKeys })
                      },
              }),
            },
            createEnvironmentApiKey: {
              mutationOptions: () => ({
                mutationFn: (variables: unknown) => apiMocks.createEnvironment(variables),
              }),
            },
            deleteEnvironmentApiKey: {
              mutationOptions: () => ({
                mutationFn: (variables: unknown) => apiMocks.deleteEnvironment(variables),
              }),
            },
          },
        },
      },
    },
  }
})

const mockCurrentWorkspace = vi.fn().mockReturnValue({
  id: 'workspace-1',
  name: 'Test Workspace',
})

vi.mock('@/context/workspace-state', async () => {
  const { createWorkspaceStateModuleMock } = await import('@/test/console/state-fixture')
  return createWorkspaceStateModuleMock(() => ({
    currentWorkspace: mockCurrentWorkspace(),
    isCurrentWorkspaceManager: true,
  }))
})

vi.mock('@/hooks/use-timestamp', () => ({
  default: () => ({
    formatTime: (value: number) => `Formatted: ${value}`,
  }),
}))

const appScope = { type: 'app', appId: 'app-123' } as const
const datasetScope = { type: 'dataset' } as const
const environmentScope = {
  type: 'environment',
  appId: 'app-123',
  environmentId: 'staging',
} as const

async function renderModal(
  scope: ComponentProps<typeof ApiKeyModal>['scope'],
  overrides: { canManage?: boolean } = {},
) {
  const queryClient = createTestQueryClient()
  const onOpenChange = vi.fn()
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ApiKeyModal
        open
        canManage={overrides.canManage ?? true}
        scope={scope}
        onOpenChange={onOpenChange}
      />
    </QueryClientProvider>,
  )
  await act(async () => {
    vi.runAllTimers()
  })
  return { ...result, onOpenChange, queryClient }
}

async function confirmKeyDeletion(accessibleName: string) {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  const deleteButton = screen.getByRole('button', { name: accessibleName })
  await user.click(deleteButton)
  await act(async () => {
    vi.runAllTimers()
  })
  await user.click(await screen.findByText('common.operation.confirm'))
}

describe('ApiKeyModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockCurrentWorkspace.mockReturnValue({ id: 'workspace-1', name: 'Test Workspace' })
    apiMocks.appKeys = []
    apiMocks.datasetKeys = []
    apiMocks.environmentKeys = []
    apiMocks.createApp.mockResolvedValue({ token: 'new-app-token-123' })
    apiMocks.deleteApp.mockResolvedValue(undefined)
    apiMocks.updateApp.mockResolvedValue({ id: 'app-key-1' })
    apiMocks.createDataset.mockResolvedValue({ token: 'new-dataset-token-123' })
    apiMocks.deleteDataset.mockResolvedValue(undefined)
    apiMocks.createEnvironment.mockResolvedValue({
      id: 'environment-key-2',
      token: 'env-created-secret-key-abcdefghijklmnopqrst',
      type: 'api',
      created_at: 1,
    })
    apiMocks.deleteEnvironment.mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it('loads and renders app API keys through the generated query input', async () => {
    apiMocks.appKeys = [
      {
        id: 'app-key-1',
        token: 'app-secret-token-123456789',
        type: 'app',
        created_at: 1,
      },
    ]

    await renderModal(appScope)

    expect(await screen.findByText('app...cret-token-123456789')).toBeInTheDocument()
    expect(apiMocks.listApp).toHaveBeenCalledWith({
      params: { resource_id: 'app-123' },
    })
  })

  it('loads and renders workspace dataset API keys', async () => {
    apiMocks.datasetKeys = [
      {
        id: 'dataset-key-1',
        token: 'dataset-secret-token-123456789',
        type: 'dataset',
        created_at: 1,
      },
    ]

    await renderModal(datasetScope)

    expect(await screen.findByText('dat...cret-token-123456789')).toBeInTheDocument()
    expect(apiMocks.listDataset).toHaveBeenCalled()
    // Dataset keys surface their knowledge-base scope; a key without dataset_ids reads
    // as scoped to all knowledge bases.
    expect(
      screen.getByRole('columnheader', { name: 'appApi.apiKeyModal.scope' }),
    ).toBeInTheDocument()
    expect(screen.getByText('appApi.apiKeyModal.scopeAllDatasets')).toBeInTheDocument()
  })

  it('creates an app API key through the generated mutation input and copies its secret', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)

    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    expect(apiMocks.createApp).not.toHaveBeenCalled()
    await user.click(await screen.findByRole('button', { name: 'common.operation.create' }))

    await waitFor(() => {
      expect(apiMocks.createApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123' },
        body: { description: '', day_limit_quota: -1, month_limit_quota: -1 },
      })
    })
    expect(
      await screen.findByRole('textbox', { name: 'appApi.apiKeyModal.secretKey' }),
    ).toHaveValue('new-app-token-123')
    await user.click(
      screen.getByRole('button', { name: 'appOverview.overview.appInfo.embedded.copy' }),
    )
    expect(await navigator.clipboard.readText()).toBe('new-app-token-123')
    await waitFor(() => expect(apiMocks.listApp).toHaveBeenCalledTimes(2))
  })

  it('creates a described app key with explicit finite limits', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    await user.type(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
      'Production',
    )
    const day = screen.getByRole('textbox', { name: 'extend.apiKeyModal.dayLimitItemName' })
    const month = screen.getByRole('textbox', { name: 'extend.apiKeyModal.monthLimitItemName' })
    await user.clear(day)
    await user.type(day, '25')
    await user.clear(month)
    await user.type(month, '250')
    await user.click(screen.getByRole('button', { name: 'common.operation.create' }))
    await waitFor(() =>
      expect(apiMocks.createApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123' },
        body: { description: 'Production', day_limit_quota: 25, month_limit_quota: 250 },
      }),
    )
  })

  it('edits legacy app keys using unlimited defaults and refreshes the list', async () => {
    const legacyAppKey: AppApiKeyList['data'][number] = {
      id: 'app-key-1',
      token: 'app-secret-token-123456789',
      type: 'app',
      created_at: 1,
    }
    apiMocks.appKeys = [legacyAppKey]
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    expect(await screen.findAllByText('0 / extend.apiKeyModal.noLimit')).toHaveLength(2)
    await user.click(
      screen.getByRole('button', { name: 'common.operation.edit app...cret-token-123456789' }),
    )
    expect(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.dayLimitItemName' }),
    ).toHaveValue('-1')
    expect(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.monthLimitItemName' }),
    ).toHaveValue('-1')
    await user.type(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
      'Updated',
    )
    apiMocks.updateApp.mockImplementation(async () => {
      const updatedAppKey = { ...legacyAppKey, description: 'Updated' }
      apiMocks.appKeys = [updatedAppKey]
      return updatedAppKey
    })
    await user.click(screen.getByRole('button', { name: 'common.operation.save' }))
    await waitFor(() =>
      expect(apiMocks.updateApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123' },
        body: {
          id: 'app-key-1',
          description: 'Updated',
          day_limit_quota: -1,
          month_limit_quota: -1,
        },
      }),
    )
    expect(await screen.findByText('Updated')).toBeInTheDocument()
    expect(apiMocks.createApp).not.toHaveBeenCalled()
  })

  it('keeps a failed quota edit open for retry without losing its draft', async () => {
    apiMocks.appKeys = [
      { id: 'app-key-1', token: 'app-secret-token-123456789', type: 'app', created_at: 1 },
    ]
    apiMocks.updateApp.mockRejectedValueOnce(new Error('Update failed'))
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    await user.click(
      await screen.findByRole('button', {
        name: 'common.operation.edit app...cret-token-123456789',
      }),
    )
    const description = screen.getByRole('textbox', {
      name: 'extend.apiKeyModal.descriptionPlaceholder',
    })
    await user.type(description, 'Keep this draft')
    await user.click(screen.getByRole('button', { name: 'common.operation.save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('common.api.actionFailed')
    expect(description).toHaveValue('Keep this draft')
    await user.click(screen.getByRole('button', { name: 'common.operation.save' }))
    await waitFor(() => expect(apiMocks.updateApp).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(
        screen.queryByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
      ).not.toBeInTheDocument(),
    )
    expect(apiMocks.updateApp).toHaveBeenLastCalledWith({
      params: { resource_id: 'app-123' },
      body: {
        id: 'app-key-1',
        description: 'Keep this draft',
        day_limit_quota: -1,
        month_limit_quota: -1,
      },
    })
  })

  it('prevents repeated creation and dismissal while a request is pending', async () => {
    const created = Promise.withResolvers<{ token: string }>()
    apiMocks.createApp.mockReturnValue(created.promise)
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    const create = screen.getByRole('button', { name: 'common.operation.create' })
    await user.click(create)
    await waitFor(() => expect(create).toHaveAttribute('aria-disabled', 'true'))
    await user.click(create)
    await user.keyboard('{Escape}')
    expect(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
    ).toBeDisabled()
    expect(apiMocks.createApp).toHaveBeenCalledTimes(1)
    await act(async () => created.resolve({ token: 'created-once' }))
    expect(
      await screen.findByRole('textbox', { name: 'appApi.apiKeyModal.secretKey' }),
    ).toHaveValue('created-once')
  })

  it('discards a cancelled creation draft and limits descriptions to the API maximum', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    const description = screen.getByRole('textbox', {
      name: 'extend.apiKeyModal.descriptionPlaceholder',
    })
    await user.type(description, 'a'.repeat(51))
    expect(description).toHaveValue('a'.repeat(50))
    await user.click(screen.getByRole('button', { name: 'common.operation.close' }))
    await waitFor(() =>
      expect(
        screen.queryByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
      ).not.toBeInTheDocument(),
    )
    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    expect(
      screen.getByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
    ).toHaveValue('')
    expect(apiMocks.createApp).not.toHaveBeenCalled()
  })

  it('rejects negative fractional quotas and accepts zero after correction', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
    const day = screen.getByRole('textbox', { name: 'extend.apiKeyModal.dayLimitItemName' })
    await user.clear(day)
    await user.type(day, '-0.5')
    await user.click(screen.getByRole('button', { name: 'common.operation.create' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.quota.editDialog.invalidInput',
    )
    expect(apiMocks.createApp).not.toHaveBeenCalled()
    await user.clear(day)
    await user.type(day, '0')
    await user.click(screen.getByRole('button', { name: 'common.operation.create' }))
    await waitFor(() =>
      expect(apiMocks.createApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123' },
        body: { description: '', day_limit_quota: 0, month_limit_quota: -1 },
      }),
    )
  })

  it('shows accumulated, daily and monthly app usage independently and preserves edit values', async () => {
    apiMocks.appKeys = [
      {
        id: 'app-key-1',
        token: 'app-secret-token-123456789',
        type: 'app',
        created_at: 1,
        description: 'Existing',
        accumulated_quota: 100,
        day_used_quota: 2,
        day_limit_quota: -1,
        month_used_quota: 20,
        month_limit_quota: 50,
      },
    ]
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(appScope)
    expect(await screen.findByText('100')).toBeInTheDocument()
    expect(screen.getByText('2 / extend.apiKeyModal.noLimit')).toBeInTheDocument()
    expect(screen.getByText('20 / 50')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: 'common.operation.edit app...cret-token-123456789' }),
    )
    const description = screen.getByRole('textbox', {
      name: 'extend.apiKeyModal.descriptionPlaceholder',
    })
    expect(description).toHaveValue('Existing')
    await user.clear(description)
    await user.click(screen.getByRole('button', { name: 'common.operation.save' }))
    await waitFor(() =>
      expect(apiMocks.updateApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123' },
        body: { id: 'app-key-1', description: '', day_limit_quota: -1, month_limit_quota: 50 },
      }),
    )
  })

  it.each([datasetScope, environmentScope])(
    'does not expose app quota controls for $type keys',
    async (scope) => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      await renderModal(scope)
      expect(
        screen.queryByRole('columnheader', { name: /extend.apiKeyModal.dayLimit/ }),
      ).not.toBeInTheDocument()
      await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))
      expect(
        screen.queryByRole('textbox', { name: 'extend.apiKeyModal.dayLimitItemName' }),
      ).not.toBeInTheDocument()
      expect(
        screen.queryByRole('textbox', { name: 'extend.apiKeyModal.descriptionPlaceholder' }),
      ).not.toBeInTheDocument()
      expect(apiMocks.createApp).not.toHaveBeenCalled()
      expect(apiMocks.updateApp).not.toHaveBeenCalled()
    },
  )

  it('creates a workspace dataset API key scoped to all knowledge bases', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(datasetScope)

    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))

    // Dataset creation first opens the scope dialog; the default "all" scope creates a
    // key with an empty dataset_ids list.
    await user.click(await screen.findByRole('button', { name: 'common.operation.create' }))

    await waitFor(() => {
      expect(apiMocks.createDataset).toHaveBeenCalledWith({ body: { dataset_ids: [] } })
    })
    expect(
      await screen.findByRole('textbox', { name: 'appApi.apiKeyModal.secretKey' }),
    ).toHaveValue('new-dataset-token-123')
  })

  it('deletes an app API key through the generated mutation input', async () => {
    apiMocks.appKeys = [
      {
        id: 'app-key-0',
        token: 'other-app-secret-token-987654321',
        type: 'app',
        created_at: 1,
      },
      {
        id: 'app-key-1',
        token: 'app-secret-token-123456789',
        type: 'app',
        created_at: 1,
      },
    ]
    await renderModal(appScope)
    await screen.findByText('app...cret-token-123456789')

    await confirmKeyDeletion('common.operation.delete app...cret-token-123456789')

    await waitFor(() => {
      expect(apiMocks.deleteApp).toHaveBeenCalledWith({
        params: { resource_id: 'app-123', api_key_id: 'app-key-1' },
      })
    })
  })

  it('deletes a dataset API key through the generated mutation input', async () => {
    apiMocks.datasetKeys = [
      {
        id: 'dataset-key-0',
        token: 'other-dataset-secret-token-987654321',
        type: 'dataset',
        created_at: 1,
      },
      {
        id: 'dataset-key-1',
        token: 'dataset-secret-token-123456789',
        type: 'dataset',
        created_at: 1,
      },
    ]
    await renderModal(datasetScope)
    await screen.findByText('dat...cret-token-123456789')

    await confirmKeyDeletion('common.operation.delete dat...cret-token-123456789')

    await waitFor(() => {
      expect(apiMocks.deleteDataset).toHaveBeenCalledWith({
        params: { api_key_id: 'dataset-key-1' },
      })
    })
  })

  it('loads environment-scoped keys without requesting built-in app keys', async () => {
    apiMocks.environmentKeys = [
      {
        id: 'environment-key-1',
        token: 'env-existing-secret-key-abcdefghijklmnopqrst',
        type: 'api',
        created_at: 1,
      },
    ]

    await renderModal(environmentScope)

    expect(await screen.findByText(/^env\.\.\./)).toBeInTheDocument()
    expect(apiMocks.listEnvironment).toHaveBeenCalledWith({
      params: {
        app_id: 'app-123',
        environment_id: 'staging',
      },
    })
    expect(apiMocks.listApp).not.toHaveBeenCalled()
  })

  it('creates an environment-scoped API key', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await renderModal(environmentScope)

    await user.click(screen.getByText('appApi.apiKeyModal.createNewSecretKey'))

    await waitFor(() => {
      expect(apiMocks.createEnvironment).toHaveBeenCalledWith({
        params: {
          app_id: 'app-123',
          environment_id: 'staging',
        },
      })
    })
    expect(
      await screen.findByRole('textbox', { name: 'appApi.apiKeyModal.secretKey' }),
    ).toHaveValue('env-created-secret-key-abcdefghijklmnopqrst')
  })

  it('deletes an environment-scoped API key', async () => {
    apiMocks.environmentKeys = [
      {
        id: 'environment-key-1',
        token: 'env-existing-secret-key-abcdefghijklmnopqrst',
        type: 'api',
        created_at: 1,
      },
    ]
    await renderModal(environmentScope)

    await screen.findByText(/^env\.\.\./)
    await confirmKeyDeletion('common.operation.delete env...abcdefghijklmnopqrst')

    await waitFor(() => {
      expect(apiMocks.deleteEnvironment).toHaveBeenCalledWith({
        params: {
          api_key_id: 'environment-key-1',
          app_id: 'app-123',
          environment_id: 'staging',
        },
      })
    })
  })

  it('disables creation when the caller cannot manage keys', async () => {
    await renderModal(datasetScope, { canManage: false })

    expect(
      screen.getByRole('button', {
        name: 'appApi.apiKeyModal.createNewSecretKey',
      }),
    ).toBeDisabled()
  })

  it.each([appScope, datasetScope, environmentScope])(
    'does not expose edit or delete actions for $type keys when the caller cannot manage keys',
    async (scope) => {
      const existingKeys = [
        {
          id: 'key-1',
          token: 'existing-secret-token-123456789',
          type: scope.type === 'environment' ? 'api' : scope.type,
          created_at: 1,
        },
      ]
      apiMocks.appKeys = existingKeys
      apiMocks.datasetKeys = existingKeys
      apiMocks.environmentKeys = existingKeys

      await renderModal(scope, { canManage: false })

      expect(await screen.findByText('exi...cret-token-123456789')).toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: /^common.operation.edit / }),
      ).not.toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: /^common.operation.delete / }),
      ).not.toBeInTheDocument()
      expect(apiMocks.updateApp).not.toHaveBeenCalled()
      expect(apiMocks.deleteApp).not.toHaveBeenCalled()
      expect(apiMocks.deleteDataset).not.toHaveBeenCalled()
      expect(apiMocks.deleteEnvironment).not.toHaveBeenCalled()
    },
  )

  it('exposes an accessible close button', async () => {
    const { onOpenChange } = await renderModal(datasetScope)
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })

    await user.click(screen.getByRole('button', { name: 'common.operation.close' }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
