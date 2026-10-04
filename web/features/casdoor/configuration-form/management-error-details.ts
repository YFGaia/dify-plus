const safeCodes = new Set([
  'authorization_pending',
  'config_conflict',
  'identity_conflict',
  'invalid_transaction',
  'invitation_mismatch',
  'not_configured',
  'provider_unavailable',
  'remote_account_disabled',
  'role_snapshot_unknown',
  'workspace_unavailable',
  'invalid_configuration',
  'forbidden',
  'unauthorized',
])

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export function safeManagementError(error: unknown) {
  const result: {
    code?: string
    reason?: 'deployment_proof_missing' | 'live_test_not_wired'
    correlationId?: string
    unauthorized: boolean
  } = {
    unauthorized: false,
  }
  if (!record(error)) return result
  const data = record(error.data) ? error.data : undefined
  const candidates = [error, data, error.body, data?.body]
  for (const candidate of candidates) {
    if (!record(candidate)) continue
    if (typeof candidate.code === 'string' && safeCodes.has(candidate.code))
      result.code = candidate.code
    if (
      candidate.reason === 'deployment_proof_missing' ||
      candidate.reason === 'live_test_not_wired'
    )
      result.reason = candidate.reason
    if (
      typeof candidate.correlation_id === 'string' &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        candidate.correlation_id,
      )
    )
      result.correlationId = candidate.correlation_id
    if (candidate.status === 401 || candidate.status === 403) result.unauthorized = true
  }
  if (result.code === 'unauthorized' || result.code === 'forbidden') result.unauthorized = true
  return result
}
