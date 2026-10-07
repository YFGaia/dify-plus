import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NuqsTestingAdapter } from 'nuqs/adapters/testing'
import { describe, expect, it, vi } from 'vite-plus/test'
import { renderWithConsoleQuery } from '@/test/console/query-data'
import SystemIntegrationPage from '../page'

// Global access is covered by the composed management admission suite.
vi.mock('@/features/system-management/access', () => ({
  useSystemManagementAccess: () => ({ canManageSystem: true }),
}))

vi.mock('@/features/casdoor/management-access/use-casdoor-management-access', () => ({
  useCasdoorManagementAccess: () => ({ canManageCasdoor: true, isPending: false }),
}))

vi.mock('@/features/casdoor/configuration-form', () => ({
  CasdoorConfigurationForm: () => <div>Casdoor configuration content</div>,
}))

const renderPage = () =>
  renderWithConsoleQuery(
    <NuqsTestingAdapter hasMemory>
      <SystemIntegrationPage />
    </NuqsTestingAdapter>,
  )

// The page owns tab selection; each child owns its configuration and service calls.
vi.mock('../dingtalk-config', () => ({
  default: () => <div>DingTalk configuration content</div>,
}))

vi.mock('../oauth2-config', () => ({
  default: () => <div>OAuth2 configuration content</div>,
}))

vi.mock('../forward-token-list', () => ({
  default: () => <div>Forward Token list content</div>,
}))

describe('SystemIntegrationPage', () => {
  it('puts Casdoor first and opens its configuration by default', () => {
    renderPage()

    expect(screen.getByText('Casdoor configuration content')).toBeVisible()
    expect(screen.getAllByRole('button')[0]).toHaveAccessibleName(/casdoor\.title$/)
    expect(screen.getByRole('button', { name: /casdoor\.title$/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.queryByText('DingTalk configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /emailApi\.title$/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /forwardToken\.title$/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()
  })

  it('replaces the content when switching tabs and returning to DingTalk', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(screen.getByRole('button', { name: /oauth2\.title$/ }))

    expect(screen.getByText('OAuth2 configuration content')).toBeVisible()
    expect(screen.queryByText('DingTalk configuration content')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /emailApi\.title$/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()

    expect(screen.queryByRole('button', { name: /forwardToken\.title$/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /dingtalk\.title$/ }))

    expect(screen.getByText('DingTalk configuration content')).toBeVisible()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /emailApi\.title$/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()
  })
})
