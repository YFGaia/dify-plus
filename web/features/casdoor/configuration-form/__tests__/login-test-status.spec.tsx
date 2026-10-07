import type {
  CasdoorConfiguration,
  CasdoorRevisionResponse,
  CasdoorValidationSummaryResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vite-plus/test'
import { LoginTestStatus } from '../login-test-status'

vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})

const configuration: CasdoorConfiguration = {
  application: 'synthetic-app',
  organization: 'synthetic-org',
  client_id: 'synthetic-client',
  backend_api_url: 'https://idp.example.test',
  browser_frontend_url: 'https://idp.example.test',
  expected_issuer: 'https://idp.example.test',
  default_workspace_id: '11111111-1111-4111-8111-111111111111',
  button_text: 'Casdoor',
}
const revisionId = '22222222-2222-4222-8222-222222222222'

function revision(summary?: CasdoorValidationSummaryResponse): CasdoorRevisionResponse {
  return {
    configuration,
    namespace_id: '44444444-4444-4444-8444-444444444444',
    revision_id: revisionId,
    secret_configured: true,
    validation: summary ? [summary] : [],
  }
}

function diagnostic(
  status: CasdoorValidationSummaryResponse['status'],
  overrides: Partial<CasdoorValidationSummaryResponse> = {},
): CasdoorValidationSummaryResponse {
  return {
    kind: 'diagnostic',
    revision_id: revisionId,
    status,
    checked_at: '2026-10-05T23:59:00Z',
    expires_at: '2026-10-06T00:00:01Z',
    correlation_id: '55555555-5555-4555-8555-555555555555',
    ...overrides,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('saved login-test status', () => {
  it.each([
    ['failed', 'Failed'],
    ['unknown', 'Unknown'],
    ['expired', 'Expired'],
    ['not_run', 'Not run'],
  ] as const)('shows the backend %s result', (status, label) => {
    render(<LoginTestStatus revision={revision(diagnostic(status))} />)
    expect(screen.getByRole('status')).toHaveTextContent(`Test login: ${label}`)
  })

  it('offers safe expandable recovery help and a correlation ID for a failed saved test', () => {
    render(<LoginTestStatus revision={revision(diagnostic('failed'))} />)
    expect(screen.getByText('Sign-in diagnostic preview')).toBeInTheDocument()
    expect(screen.getByText('Sign-in verification failed. Test login again.')).toBeInTheDocument()
    expect(
      screen.getByText(
        'If Casdoor cannot provide sign-in verification information, ask your administrator to check the application and version.',
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Correlation ID: 55555555-5555-4555-8555-555555555555'),
    ).toBeInTheDocument()
  })

  it('shows Passed only while the saved diagnostic is still current', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-06T00:00:00Z'))
    render(<LoginTestStatus revision={revision(diagnostic('passed'))} />)
    expect(screen.getByRole('status')).toHaveTextContent('Test login: Passed')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(screen.getByRole('status')).toHaveTextContent('Test login: Expired')
  })

  it('shows Unknown for incomplete timestamps and ignores a summary from another revision', () => {
    const incomplete = revision(diagnostic('passed', { expires_at: null }))
    render(<LoginTestStatus revision={incomplete} />)
    expect(screen.getByRole('status')).toHaveTextContent('Test login: Unknown')

    const mismatched = revision(
      diagnostic('passed', { revision_id: '33333333-3333-4333-8333-333333333333' }),
    )
    render(<LoginTestStatus revision={mismatched} />)
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})
