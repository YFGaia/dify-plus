import type { DingTalkConfigResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import DingTalkConfig from '../dingtalk-config'

const api = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), test: vi.fn(), lookup: vi.fn() }))
vi.mock('@/service/console', () => ({
  consoleClient: {
    systemManageExtend: {
      integration: {
        dingtalk: { get: api.get, post: api.save, test: { get: api.test } },
        emailApi: { test: { post: api.lookup } },
      },
    },
  },
}))

const fixture = (config: Record<string, unknown> = {}): DingTalkConfigResponse => ({
  status: true,
  corp_id: 'corp',
  agent_id: 'agent',
  app_id: '',
  app_key: 'key',
  app_secret: 'secret',
  config,
})
const renderConfig = async (config: Record<string, unknown> = {}) => {
  api.get.mockResolvedValue(fixture(config))
  render(<DingTalkConfig />)
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
    api.save.mockResolvedValue({ result: 'success' })
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
    await waitFor(() => expect(api.save).toHaveBeenCalledWith({ body: fixture(expected) }))
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
      expect(api.lookup).toHaveBeenCalledWith({
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
      }),
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
    expect(await screen.findByRole('alert')).toHaveTextContent('No valid email at configured path')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
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
      expect(api.save).toHaveBeenCalledWith({
        body: expect.objectContaining({
          agent_id: 'new-agent',
          config: expect.objectContaining({
            other: { value: 1 },
            email_api: expect.objectContaining(existing.email_api),
          }),
        }),
      }),
    )
  })
})
