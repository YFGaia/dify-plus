import type { DingTalkConfigResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import DingTalkConfig from '../dingtalk-config'

const api = vi.hoisted(() => ({
  get: vi.fn(),
  save: vi.fn(),
  test: vi.fn(),
  lookup: vi.fn(),
  start: vi.fn(),
  poll: vi.fn(),
  cancel: vi.fn(),
  diagnostic: vi.fn(),
  enable: vi.fn(),
  disable: vi.fn(),
  canManage: true,
  params: new URLSearchParams(),
}))
vi.mock('@/features/system-management/access', () => ({
  useSystemManagementAccess: () => ({ canManageSystem: api.canManage }),
}))
vi.mock('@/next/navigation', () => ({ useSearchParams: () => api.params }))
vi.mock('@/context/workspace-state', async () => {
  const { atom } = await import('jotai')
  return { currentWorkspaceIdAtom: atom('workspace-id') }
})
vi.mock('@/service/console', async () => {
  const { createTanstackQueryUtils } = await import('@orpc/tanstack-query')
  const client = {
    systemManageExtend: {
      integration: {
        dingtalk: {
          get: api.get,
          post: api.save,
          test: { get: api.test },
          enable: { post: api.enable },
          disable: { post: api.disable },
          diagnostics: { post: api.diagnostic },
          registration: {
            start: { post: api.start },
            poll: { post: api.poll },
            byTransactionId: { delete: api.cancel },
          },
        },
        emailApi: { test: { post: api.lookup } },
      },
    },
  }
  return { consoleClient: client, consoleQuery: createTanstackQueryUtils(client) }
})
const mount = () =>
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DingTalkConfig />
    </QueryClientProvider>,
  )

const fixture = (config: Record<string, unknown> = {}): DingTalkConfigResponse => ({
  status: false,
  corp_id: 'corp',
  agent_id: 'agent',
  app_id: '',
  app_key: 'key',
  app_secret: '',
  revision: 'revision-1',
  secret_configured: true,
  connection_verified: false,
  verified: false,
  callback_url: 'https://example.com/console/api/auth/dingtalk/callback',
  config,
})
const renderConfig = async (config: Record<string, unknown> = {}) => {
  api.get.mockResolvedValue(fixture(config))
  mount()
  await screen.findByRole('radio', { name: 'extend.systemManage.dingtalk.manualSetup' })
  await userEvent
    .setup()
    .click(screen.getByRole('radio', { name: 'extend.systemManage.dingtalk.manualSetup' }))
  await screen.findByLabelText('extend.systemManage.dingtalk.corpId')
}
const enableLookup = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(
    screen.getByRole('switch', { name: 'extend.systemManage.dingtalk.emailLookup.enable' }),
  )
  await user.type(
    screen.getByLabelText('extend.systemManage.emailApi.url'),
    'https://example.com/email',
  )
}

describe('DingTalk email lookup configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.canManage = true
    api.params = new URLSearchParams()
    api.save.mockResolvedValue(fixture())
    api.lookup.mockResolvedValue({ result: 'success', email: 'employee@example.com' })
  })

  it('saves lookup with DingTalk and reloads while preserving existing extensions and advanced settings', async () => {
    const user = userEvent.setup()
    const existing = {
      custom_feature: { enabled: true },
      email_api: {
        enabled: true,
        url: 'https://example.com/email',
        method: 'PUT',
        body_type: 'form-data',
        request_param_field: 'employeeId',
        response_email_field: 'employee.mail',
        headers: { 'X-Company': 'company' },
        body_data: { form_data: [{ key: 'department', value: 'sales' }] },
        authorization: {
          type: 'basic',
          username: 'service',
          password: 'password',
          custom: 'retained',
        },
        provider_extension: { value: 42 },
      },
    }
    await renderConfig(existing)
    await user.clear(screen.getByLabelText('extend.systemManage.emailApi.url'))
    await user.type(
      screen.getByLabelText('extend.systemManage.emailApi.url'),
      'https://example.com/new-email',
    )
    const expected = {
      ...existing,
      email_api: { ...existing.email_api, url: 'https://example.com/new-email' },
    }
    api.get.mockResolvedValue(fixture(expected))
    await user.click(screen.getByRole('button', { name: 'extend.systemManage.common.save' }))
    await waitFor(() =>
      expect(api.save).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({ config: expected, expected_revision: 'revision-1' }),
        }),
        expect.anything(),
      ),
    )
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    expect(await screen.findByLabelText('extend.systemManage.emailApi.url')).toHaveValue(
      'https://example.com/new-email',
    )
  })

  it('tests current unsaved settings with a DingTalk user ID and never saves', async () => {
    const user = userEvent.setup()
    await renderConfig()
    await enableLookup(user)
    await user.type(
      screen.getByLabelText('extend.systemManage.dingtalk.emailLookup.userId'),
      'ding-user-123',
    )
    await user.click(
      screen.getByRole('button', { name: 'extend.systemManage.dingtalk.emailLookup.test' }),
    )
    await waitFor(() =>
      expect(api.lookup).toHaveBeenCalledWith(
        {
          body: {
            user_id: 'ding-user-123',
            config: {
              enabled: true,
              url: 'https://example.com/email',
              method: 'GET',
              request_param_field: 'userId',
              response_email_field: 'data[0].userName',
              headers: {},
              body_data: {},
            },
          },
        },
        { context: { silent: true } },
      ),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('employee@example.com')
    expect(api.save).not.toHaveBeenCalled()
  })

  it.each(['[]', 'broken'])('rejects invalid advanced JSON %s before save', async (value) => {
    const user = userEvent.setup()
    await renderConfig({ email_api: { enabled: true, url: 'https://example.com/email' } })
    await user.click(screen.getByText('extend.systemManage.dingtalk.emailLookup.advanced'))
    const headers = screen.getByLabelText('extend.systemManage.dingtalk.emailLookup.headers')
    await user.clear(headers)
    await user.click(headers)
    await user.paste(value)
    await user.click(screen.getByRole('button', { name: 'extend.systemManage.common.save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.emailLookup.validation',
    )
    expect(api.save).not.toHaveBeenCalled()
  })

  it('requires a test user ID and reports an unsuccessful lookup without a success result', async () => {
    const user = userEvent.setup()
    await renderConfig()
    await enableLookup(user)
    await user.click(
      screen.getByRole('button', { name: 'extend.systemManage.dingtalk.emailLookup.test' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.emailLookup.validation',
    )
    expect(api.lookup).not.toHaveBeenCalled()
    await user.type(
      screen.getByLabelText('extend.systemManage.dingtalk.emailLookup.userId'),
      'ding-user-123',
    )
    api.lookup.mockResolvedValue({
      result: 'error',
      message: 'No valid email at configured path',
      email: null,
    })
    await user.click(
      screen.getByRole('button', { name: 'extend.systemManage.dingtalk.emailLookup.test' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.common.testFailed',
    )
    expect(
      screen.queryByText('extend.systemManage.dingtalk.emailLookup.testSuccess'),
    ).not.toBeInTheDocument()
  })

  it('keeps disabled existing settings when saving unrelated DingTalk fields', async () => {
    const user = userEvent.setup()
    const existing = {
      email_api: {
        enabled: false,
        url: 'https://example.com/email',
        authorization: { type: 'bearer', token: 'token' },
        custom: true,
      },
      other: { value: 1 },
    }
    await renderConfig(existing)
    expect(screen.queryByLabelText('extend.systemManage.emailApi.url')).not.toBeInTheDocument()
    await user.clear(screen.getByLabelText('extend.systemManage.dingtalk.agentId'))
    await user.type(screen.getByLabelText('extend.systemManage.dingtalk.agentId'), 'new-agent')
    await user.click(screen.getByRole('button', { name: 'extend.systemManage.common.save' }))
    await waitFor(() =>
      expect(api.save).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            agent_id: 'new-agent',
            config: expect.objectContaining({
              other: { value: 1 },
              email_api: expect.objectContaining(existing.email_api),
            }),
          }),
        }),
        expect.anything(),
      ),
    )
  })
})

describe('DingTalk verified onboarding', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.canManage = true
    api.params = new URLSearchParams()
    api.get.mockResolvedValue(fixture())
  })

  it('defaults to QR setup with no credential inputs and keeps activation unavailable before login verification', async () => {
    mount()
    expect(
      await screen.findByRole('radio', { name: 'extend.systemManage.dingtalk.qrSetup' }),
    ).toBeChecked()
    expect(screen.queryByLabelText('extend.systemManage.dingtalk.appKey')).not.toBeInTheDocument()
    expect(
      screen.queryByLabelText('extend.systemManage.dingtalk.appSecret'),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'extend.systemManage.common.enable' })).toBeDisabled()
    expect(screen.getByLabelText('extend.systemManage.dingtalk.callbackUrl')).toHaveValue(
      'https://example.com/console/api/auth/dingtalk/callback',
    )
    expect(
      screen.getByRole('link', { name: 'extend.systemManage.dingtalk.developerConsole' }),
    ).toHaveAttribute('href', 'https://open-dev.dingtalk.com/')
    expect(screen.getByText('extend.systemManage.dingtalk.permissionsHint')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'extend.systemManage.common.enable' }),
    ).toHaveAccessibleDescription('extend.systemManage.dingtalk.enableHint')
  })

  it('copies the server callback URL in either setup mode and explains a clipboard failure', async () => {
    const user = userEvent.setup()
    const write = vi.spyOn(navigator.clipboard, 'writeText')
    try {
      mount()
      await user.click(
        await screen.findByRole('button', {
          name: 'extend.systemManage.dingtalk.copyCallback',
        }),
      )
      expect(write).toHaveBeenCalledWith('https://example.com/console/api/auth/dingtalk/callback')
      await user.click(
        screen.getByRole('radio', { name: 'extend.systemManage.dingtalk.manualSetup' }),
      )
      expect(screen.getByText('extend.systemManage.dingtalk.permissionsHint')).toBeInTheDocument()
      write.mockRejectedValueOnce(new Error('clipboard unavailable'))
      await user.click(
        screen.getByRole('button', { name: 'extend.systemManage.dingtalk.copyCallback' }),
      )
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'extend.systemManage.dingtalk.copyFailed',
      )
      expect(api.enable).not.toHaveBeenCalled()
    } finally {
      write.mockRestore()
    }
  })

  it('saves manually supplied credentials without requesting a QR code or enabling login', async () => {
    const user = userEvent.setup()
    await renderConfig()
    await user.type(screen.getByLabelText('extend.systemManage.dingtalk.appSecret'), 'new-secret')
    await user.click(screen.getByRole('button', { name: 'extend.systemManage.common.save' }))
    await waitFor(() => expect(api.save).toHaveBeenCalled())
    expect(api.save.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        body: expect.objectContaining({
          app_secret: 'new-secret',
          expected_revision: 'revision-1',
        }),
      }),
    )
    expect(api.save.mock.calls[0]?.[0].body).not.toHaveProperty('status')
    expect(api.start).not.toHaveBeenCalled()
    expect(api.enable).not.toHaveBeenCalled()
  })

  it('obtains a QR code without credentials, polls at the server interval and refreshes after automatic credential acquisition', async () => {
    const user = userEvent.setup()
    api.start.mockResolvedValue({
      transaction_id: 'transaction-1',
      verification_uri_complete: 'https://login.dingtalk.com/qr?ticket=example',
      expires_in: 30,
      interval: 1,
      status: 'WAITING',
    })
    api.poll.mockResolvedValue({ transaction_id: 'transaction-1', status: 'SUCCESS' })
    mount()
    await screen.findByRole('button', { name: 'extend.systemManage.dingtalk.createQr' })
    await user.click(screen.getByRole('button', { name: 'extend.systemManage.dingtalk.createQr' }))
    expect(screen.getByRole('status')).toHaveTextContent(
      'extend.systemManage.dingtalk.registration.WAITING',
    )
    expect(api.start.mock.calls[0]?.[0]).toEqual({})
    expect(api.poll).not.toHaveBeenCalled()
    await waitFor(() => expect(api.poll).toHaveBeenCalled(), { timeout: 2000 })
    await screen.findByText('extend.systemManage.dingtalk.registration.SUCCESS')
    expect(api.poll.mock.calls[0]?.[0]).toEqual({ body: { transaction_id: 'transaction-1' } })
    expect(screen.getByRole('status')).toHaveTextContent(
      'extend.systemManage.dingtalk.registration.SUCCESS',
    )
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'extend.systemManage.common.enable' })).toBeDisabled()
    expect(screen.getByText('extend.systemManage.dingtalk.enableHint')).toBeInTheDocument()
    expect(api.enable).not.toHaveBeenCalled()
  })

  it('cancels an active registration and shows its terminal state', async () => {
    const user = userEvent.setup()
    api.start.mockResolvedValue({
      transaction_id: 'transaction-1',
      verification_uri_complete: 'https://login.dingtalk.com/qr?ticket=example',
      expires_in: 30,
      interval: 10,
      status: 'WAITING',
    })
    api.cancel.mockResolvedValue({ transaction_id: 'transaction-1', status: 'CANCELLED' })
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'extend.systemManage.dingtalk.createQr' }),
    )
    await user.click(
      await screen.findByRole('button', { name: 'extend.systemManage.common.cancel' }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(
      'extend.systemManage.dingtalk.registration.CANCELLED',
    )
    expect(api.cancel.mock.calls[0]?.[0]).toEqual({ params: { transaction_id: 'transaction-1' } })
  })

  it('requires saving drafts before verification and enables only a verified saved configuration', async () => {
    const user = userEvent.setup()
    api.get.mockResolvedValue({ ...fixture(), connection_verified: true, verified: true })
    api.enable.mockResolvedValue({
      ...fixture(),
      status: true,
      connection_verified: true,
      verified: true,
    })
    mount()
    const enable = await screen.findByRole('button', { name: 'extend.systemManage.common.enable' })
    expect(enable).toBeEnabled()
    await user.click(enable)
    await waitFor(() => expect(api.enable).toHaveBeenCalled())
    await user.click(
      screen.getByRole('radio', { name: 'extend.systemManage.dingtalk.manualSetup' }),
    )
    await user.type(screen.getByLabelText('extend.systemManage.dingtalk.appKey'), '-changed')
    expect(screen.getByRole('button', { name: 'extend.systemManage.common.enable' })).toBeDisabled()
    expect(
      screen.getByRole('button', { name: 'extend.systemManage.dingtalk.verifyLogin' }),
    ).toBeDisabled()
    expect(screen.getByText('extend.systemManage.dingtalk.saveFirst')).toBeInTheDocument()
  })

  it('starts a browser-bound real diagnostic with explicit account binding disabled by default', async () => {
    const user = userEvent.setup()
    api.get.mockResolvedValue({ ...fixture(), connection_verified: true })
    api.diagnostic.mockResolvedValue({
      authorization_url: 'https://login.dingtalk.com/oauth2/auth?state=example',
    })
    const navigate = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    try {
      mount()
      const verify = await screen.findByRole('button', {
        name: 'extend.systemManage.dingtalk.verifyLogin',
      })
      await user.click(verify)
      await waitFor(() =>
        expect(navigate).toHaveBeenCalledWith(
          'https://login.dingtalk.com/oauth2/auth?state=example',
        ),
      )
      expect(api.diagnostic.mock.calls[0]?.[0]).toEqual({ body: { bind_current_account: false } })
      await user.click(
        screen.getByRole('checkbox', { name: 'extend.systemManage.dingtalk.bindCurrentAccount' }),
      )
      await user.click(verify)
      await waitFor(() => expect(api.diagnostic).toHaveBeenCalledTimes(2))
      expect(api.diagnostic.mock.calls[1]?.[0]).toEqual({ body: { bind_current_account: true } })
      expect(api.enable).not.toHaveBeenCalled()
    } finally {
      navigate.mockRestore()
    }
  })

  it('uses a dedicated disable mutation for an enabled integration', async () => {
    const user = userEvent.setup()
    api.get.mockResolvedValue({
      ...fixture(),
      status: true,
      connection_verified: true,
      verified: true,
    })
    api.disable.mockResolvedValue(fixture())
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'extend.systemManage.dingtalk.disable' }),
    )
    await waitFor(() => expect(api.disable).toHaveBeenCalled())
    expect(api.save).not.toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('does not treat a fabricated callback success query as verified server state', async () => {
    api.params = new URLSearchParams('dingtalk_result=success')
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.loginFailed',
    )
    expect(screen.queryByText('extend.systemManage.dingtalk.loginVerified')).not.toBeInTheDocument()
  })

  it('does not load global settings without management permission', async () => {
    api.canManage = false
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.common.noPermission',
    )
    expect(api.get).not.toHaveBeenCalled()
    expect(api.start).not.toHaveBeenCalled()
  })
})
