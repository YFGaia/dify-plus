import type { CasdoorRevisionResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vite-plus/test'
import { parseServerConfiguration } from '../configuration-draft'
import { parseDiagnosticStart } from '../diagnostic-navigation'
import { DiagnosticStatus } from '../diagnostic-status'

const { api } = vi.hoisted(() => ({ api: { prefix: 'https://console.example.test/console/api' } }))
vi.mock('@/config', () => ({
  get API_PREFIX() {
    return api.prefix
  },
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})
const revisionId = '22222222-2222-4222-8222-222222222222'
const namespaceId = '44444444-4444-4444-8444-444444444444'
const correlationId = '55555555-5555-4555-8555-555555555555'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const handoff = `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`
function revision(): CasdoorRevisionResponse {
  return {
    revision_id: revisionId,
    namespace_id: namespaceId,
    secret_configured: true,
    configuration: {
      organization: 'synthetic-org',
      application: 'synthetic-app',
      client_id: 'client',
      default_workspace_id: workspaceId,
      backend_api_url: 'https://idp.example.test',
      browser_frontend_url: 'https://idp.example.test',
      expected_issuer: 'https://idp.example.test',
    },
    validation: [
      {
        kind: 'diagnostic',
        revision_id: revisionId,
        status: 'passed',
        correlation_id: correlationId,
        checked_at: '2026-10-05T00:00:00Z',
        expires_at: '2026-10-05T00:15:00Z',
      },
    ],
    diagnostic: {
      revision_id: revisionId,
      namespace_id: namespaceId,
      correlation_id: correlationId,
      effective_role_count: 3,
      stages: [{ stage: 'protocol', status: 'passed' }],
      targets: [{ workspace_id: workspaceId, target_role: 'editor', reason: 'role_mapping' }],
    },
  }
}
afterEach(() => {
  vi.useRealTimers()
  api.prefix = 'https://console.example.test/console/api'
})

describe('diagnostic handoff authority', () => {
  it('resolves only the opaque local handler against the configured Console API origin', () => {
    expect(
      parseDiagnosticStart({ status: 'started', reason: null, handoff: { handoff_path: handoff } })
        .destination,
    ).toBe(`https://console.example.test${handoff}`)
    api.prefix = '/console/api'
    expect(
      parseDiagnosticStart({ status: 'started', handoff: { handoff_path: handoff } }).destination,
    ).toBe(`${window.location.origin}${handoff}`)
    expect(
      parseDiagnosticStart({ status: 'blocked', reason: 'deployment_proof_missing', handoff: null })
        .destination,
    ).toBeNull()
  })
  it.each([
    `https://evil.example${handoff}`,
    `//evil.example${handoff}`,
    `${handoff}?next=evil`,
    `${handoff}#x`,
    '/console/api/auth/casdoor/diagnostic/short',
    '/console/api/auth/casdoor/diagnostic/%2F',
  ])('rejects untrusted handoff %s', (handoff_path) => {
    expect(() => parseDiagnosticStart({ status: 'started', handoff: { handoff_path } })).toThrow()
  })
  it.each([
    { status: 'started' },
    { status: 'started', reason: 'deployment_proof_missing', handoff: { handoff_path: handoff } },
    { status: 'blocked', reason: 'deployment_proof_missing', handoff: { handoff_path: handoff } },
    { status: 'blocked', reason: 'arbitrary unsafe message' },
  ])('rejects contradictory or unsafe response %j', (response) => {
    expect(() => parseDiagnosticStart(response)).toThrow()
  })
  it.each([
    'https://user:password@console.example.test/console/api',
    'https://console.example.test/console/api?x=1',
    'https://console.example.test/other',
    'javascript:alert(1)',
  ])('rejects invalid API configuration %s', (prefix) => {
    api.prefix = prefix
    expect(() =>
      parseDiagnosticStart({ status: 'started', handoff: { handoff_path: handoff } }),
    ).toThrow()
  })
})

describe('read-only diagnostic preview', () => {
  it('expires a displayed successful diagnostic at its deadline and refreshes once', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T00:14:59Z'))
    const refresh = vi.fn()
    render(<DiagnosticStatus revision={revision()} onExpire={refresh} />)
    expect(screen.getByRole('region', { name: 'Sign-in diagnostic preview' })).toBeInTheDocument()
    expect(screen.getByText(`${workspaceId} → Editor (editor) · Role mapping`)).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(screen.getByText('Test sign-in: Expired')).toBeInTheDocument()
    expect(
      screen.queryByRole('region', { name: 'Sign-in diagnostic preview' }),
    ).not.toBeInTheDocument()
    expect(refresh).toHaveBeenCalledOnce()
    act(() => {
      vi.advanceTimersByTime(60_000)
    })
    expect(refresh).toHaveBeenCalledOnce()
  })
  it.each(['failed', 'unknown', 'expired', 'not_run'] as const)(
    'hides prior preview when the real aggregate is %s',
    (status) => {
      const data = revision()
      data.validation![0]!.status = status
      render(<DiagnosticStatus revision={data} />)
      expect(
        screen.queryByRole('region', { name: 'Sign-in diagnostic preview' }),
      ).not.toBeInTheDocument()
    },
  )
  it.each([
    { expires_at: null },
    { expires_at: 'invalid' },
    { checked_at: null },
    { checked_at: '2026-10-05T00:16:00Z' },
  ])('shows incomplete freshness as unknown and hides the preview: %j', (overrides) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T00:00:01Z'))
    const data = revision()
    Object.assign(data.validation![0]!, overrides)
    render(<DiagnosticStatus revision={data} />)
    expect(screen.getByText('Test sign-in: Unknown')).toBeInTheDocument()
    expect(
      screen.queryByRole('region', { name: 'Sign-in diagnostic preview' }),
    ).not.toBeInTheDocument()
  })
  it('hides a preview whose correlation does not match the current diagnostic row', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T00:00:01Z'))
    const data = revision()
    data.validation![0]!.correlation_id = '66666666-6666-4666-8666-666666666666'
    render(<DiagnosticStatus revision={data} />)
    expect(
      screen.queryByRole('region', { name: 'Sign-in diagnostic preview' }),
    ).not.toBeInTheDocument()
  })
  it('rejects crossed revision/namespace summaries and duplicate aggregates at the transport boundary', () => {
    const draft = revision()
    const response = {
      etag: 1,
      enabled: false,
      draft_revision_id: revisionId,
      draft,
      active: null,
      active_revision_id: null,
    }
    expect(parseServerConfiguration(response)).not.toBeNull()
    expect(
      parseServerConfiguration({
        ...response,
        draft: { ...draft, diagnostic: { ...draft.diagnostic, namespace_id: workspaceId } },
      }),
    ).toBeNull()
    expect(
      parseServerConfiguration({
        ...response,
        draft: { ...draft, validation: [...draft.validation!, ...draft.validation!] },
      }),
    ).toBeNull()
    expect(
      parseServerConfiguration({
        ...response,
        draft: { ...draft, validation: [{ ...draft.validation![0], revision_id: workspaceId }] },
      }),
    ).toBeNull()
  })
})
