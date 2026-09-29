import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vite-plus/test'
import SystemIntegrationPage from '../page'

// The page owns tab selection; each child owns its configuration and service calls.
vi.mock('../dingtalk-config', () => ({
  default: () => <div>DingTalk configuration content</div>,
}))

vi.mock('../oauth2-config', () => ({
  default: () => <div>OAuth2 configuration content</div>,
}))

vi.mock('../email-api-config', () => ({
  default: () => <div>Email API configuration content</div>,
}))

vi.mock('../forward-token-list', () => ({
  default: () => <div>Forward Token list content</div>,
}))

describe('SystemIntegrationPage', () => {
  it('shows only DingTalk content by default', () => {
    render(<SystemIntegrationPage />)

    expect(screen.getByText('DingTalk configuration content')).toBeVisible()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Email API configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()
  })

  it('replaces the content when switching tabs and returning to DingTalk', async () => {
    const user = userEvent.setup()
    render(<SystemIntegrationPage />)

    await user.click(screen.getByRole('button', { name: /oauth2\.title$/ }))

    expect(screen.getByText('OAuth2 configuration content')).toBeVisible()
    expect(screen.queryByText('DingTalk configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Email API configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /emailApi\.title$/ }))

    expect(screen.getByText('Email API configuration content')).toBeVisible()
    expect(screen.queryByText('DingTalk configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /forwardToken\.title$/ }))

    expect(screen.getByText('Forward Token list content')).toBeVisible()
    expect(screen.queryByText('DingTalk configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Email API configuration content')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /dingtalk\.title$/ }))

    expect(screen.getByText('DingTalk configuration content')).toBeVisible()
    expect(screen.queryByText('OAuth2 configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Email API configuration content')).not.toBeInTheDocument()
    expect(screen.queryByText('Forward Token list content')).not.toBeInTheDocument()
  })
})
