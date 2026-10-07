import type { GetWorkspacesCurrentSummaryResponse } from '@dify/contracts/api/console/workspaces/types.gen'
import type { ReactElement } from 'react'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { consoleQuery } from '@/service/console'
import { seedCurrentWorkspaceQuery } from '@/test/console/current-workspace'
import { createConsoleQueryClient, renderWithConsoleQuery } from '@/test/console/query-data'
import { ApiSecretKeyButton } from '../shared/api-secret-key-button'

const request = vi.hoisted(() => vi.fn())

vi.mock('@/service/console/browser', () => ({
  consoleBrowserLink: { call: request },
}))

const appApiKeys = {
  data: [
    { id: 'key-1', token: 'app-a', type: 'app', created_at: 1, last_used_at: 1, dataset_ids: [] },
    { id: 'key-2', token: 'app-b', type: 'app', created_at: 2, last_used_at: 2, dataset_ids: [] },
  ],
}

const render = (
  ui: ReactElement,
  {
    role = 'owner',
    seedKeys = true,
  }: {
    role?: GetWorkspacesCurrentSummaryResponse['role']
    seedKeys?: boolean
  } = {},
) => {
  const queryClient = createConsoleQueryClient()
  if (seedKeys)
    queryClient.setQueryData(
      consoleQuery.apps.byResourceId.apiKeys.get.queryKey({
        input: { params: { resource_id: 'app-1' } },
      }),
      appApiKeys,
    )
  return renderWithConsoleQuery(ui, {
    queryClient,
    currentWorkspace: { role, admin_extend: false, tenant_extend: false },
  })
}

vi.mock('@/app/components/api-key/api-key-modal', () => ({
  ApiKeyModal: ({
    canManage,
    open,
    scope,
  }: {
    canManage: boolean
    open: boolean
    scope:
      | { type: 'app'; appId: string }
      | { type: 'dataset' }
      | { type: 'environment'; appId: string; environmentId: string }
  }) =>
    open ? (
      <div role="dialog" aria-label="API key management">
        {scope.type === 'dataset' ? '' : scope.appId}:
        {scope.type === 'environment' ? scope.environmentId : ''}:{String(canManage)}
      </div>
    ) : null,
}))

describe('ApiSecretKeyButton', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    request.mockResolvedValue(appApiKeys)
  })

  it.each(['owner', 'admin'] as const)(
    'lets a workspace %s with access-point permission fetch and manage keys',
    async (role) => {
      const user = userEvent.setup()
      render(<ApiSecretKeyButton appId="app-1" canManage />, { role, seedKeys: false })

      const button = await screen.findByRole('button', {
        name: 'appApi.apiKeyModal.apiSecretKey 2',
      })
      expect(button).toBeEnabled()
      expect(request).toHaveBeenCalledTimes(1)

      await user.click(button)

      expect(screen.getByRole('dialog', { name: 'API key management' })).toHaveTextContent(
        'app-1::true',
      )
    },
  )

  it('uses the environment API key count and opens environment-scoped key management', async () => {
    const user = userEvent.setup()
    render(<ApiSecretKeyButton appId="app-1" environmentId="staging" apiKeyCount={5} canManage />)

    const button = screen.getByRole('button', {
      name: 'appApi.apiKeyModal.apiSecretKey 5',
    })
    expect(button).toBeEnabled()

    await user.click(button)

    expect(screen.getByRole('dialog', { name: 'API key management' })).toHaveTextContent(
      'app-1:staging:true',
    )
  })

  it('keeps the current count visible when service access is disabled', () => {
    render(<ApiSecretKeyButton appId="app-1" canManage disabled />)

    expect(
      screen.getByRole('button', {
        name: 'appApi.apiKeyModal.apiSecretKey 2',
      }),
    ).toBeDisabled()
  })

  it('does not request or open API keys without access-point management permission', async () => {
    const user = userEvent.setup()
    render(<ApiSecretKeyButton appId="app-1" canManage={false} />, { seedKeys: false })

    const button = screen.getByRole('button', { name: 'appApi.apiKeyModal.apiSecretKey 0' })
    expect(button).toBeDisabled()
    await user.click(button)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(request).not.toHaveBeenCalled()
  })

  it.each([undefined, 'staging'])(
    'denies non-manager key access even when the app ACL allows it (environment: %s)',
    async (environmentId) => {
      await act(async () => {
        render(
          <ApiSecretKeyButton
            appId="app-1"
            environmentId={environmentId}
            apiKeyCount={5}
            canManage
          />,
          { role: 'editor', seedKeys: false },
        )
      })

      expect(screen.queryByRole('button')).not.toBeInTheDocument()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(request).not.toHaveBeenCalled()
    },
  )

  it.each(['workspace role', 'access-point ACL'] as const)(
    'removes open key management when the %s is revoked',
    async (permission) => {
      const user = userEvent.setup()
      const { queryClient, rerender } = render(<ApiSecretKeyButton appId="app-1" canManage />)
      await user.click(screen.getByRole('button', { name: 'appApi.apiKeyModal.apiSecretKey 2' }))
      expect(screen.getByRole('dialog', { name: 'API key management' })).toBeInTheDocument()

      await act(async () => {
        if (permission === 'workspace role')
          seedCurrentWorkspaceQuery(queryClient, {
            role: 'editor',
            admin_extend: false,
            tenant_extend: false,
          })
        else rerender(<ApiSecretKeyButton appId="app-1" canManage={false} />)
      })

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(request).not.toHaveBeenCalled()
    },
  )
})
