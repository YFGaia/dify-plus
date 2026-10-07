import type {
  CasdoorRevisionResponse,
  CasdoorValidationSummaryResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { useEffect, useState } from 'react'
import { useTranslation } from '#i18n'

export function DiagnosticStatus({
  revision,
  onExpire,
}: {
  revision: CasdoorRevisionResponse
  onExpire?: () => void
}) {
  const [now, setNow] = useState(Date.now)
  const summaries = revision.validation ?? []
  const effectiveStatus = (summary: CasdoorValidationSummaryResponse | undefined) => {
    if (!summary) return 'not_run'
    if (summary.status !== 'passed') return summary.status
    if (!summary.expires_at || !summary.checked_at) return 'unknown'
    const deadline = Date.parse(summary.expires_at)
    const checked = Date.parse(summary.checked_at)
    if (!Number.isFinite(deadline) || !Number.isFinite(checked) || checked > deadline)
      return 'unknown'
    return deadline <= now ? 'expired' : 'passed'
  }
  const nextExpiry = Math.min(
    ...summaries
      .filter((summary) => summary.status === 'passed' && summary.expires_at)
      .map((summary) => Date.parse(summary.expires_at!))
      .filter((deadline) => Number.isFinite(deadline) && deadline > now),
  )
  useEffect(() => {
    if (!Number.isFinite(nextExpiry)) return
    const timer = window.setTimeout(
      () => {
        setNow(Date.now())
        onExpire?.()
      },
      Math.min(Math.max(nextExpiry - Date.now(), 0), 2_147_483_647),
    )
    return () => window.clearTimeout(timer)
  }, [nextExpiry, now, onExpire])
  const { t } = useTranslation('extend')
  const statuses: Record<CasdoorValidationSummaryResponse['status'], string> = {
    passed: t(($) => $['systemManage.casdoor.diagnosticPassed']),
    failed: t(($) => $['systemManage.casdoor.diagnosticFailed']),
    unknown: t(($) => $['systemManage.casdoor.diagnosticUnknown']),
    expired: t(($) => $['systemManage.casdoor.diagnosticExpired']),
    not_run: t(($) => $['systemManage.casdoor.diagnosticNotRun']),
  }
  const stages = {
    configuration: t(($) => $['systemManage.casdoor.connection']),
    protocol: t(($) => $['systemManage.casdoor.diagnosticProtocol']),
    identity: t(($) => $['systemManage.casdoor.diagnosticIdentity']),
    online_status: t(($) => $['systemManage.casdoor.diagnosticOnline']),
    role_snapshot: t(($) => $['systemManage.casdoor.diagnosticRoles']),
    workspace: t(($) => $['systemManage.casdoor.mappingWorkspace']),
    rbac: t(($) => $['systemManage.casdoor.diagnosticPermissions']),
  }
  const preview = revision.diagnostic
  const successfulPreview =
    preview &&
    revision.validation?.some(
      (summary) =>
        summary.kind === 'diagnostic' &&
        effectiveStatus(summary) === 'passed' &&
        summary.correlation_id === preview.correlation_id,
    )
  return (
    <div className="space-y-3">
      <div aria-live="polite">
        {(['validation', 'diagnostic'] as const).map((kind) => {
          const summary = revision.validation?.find((entry) => entry.kind === kind)
          return (
            <div key={kind} className="space-y-1">
              <p>
                {t(($) => $['systemManage.casdoor.diagnosticCheck'], {
                  stage:
                    kind === 'validation'
                      ? t(($) => $['systemManage.casdoor.diagnosticValidation'])
                      : t(($) => $['systemManage.casdoor.testLogin']),
                  status: statuses[effectiveStatus(summary)],
                })}
              </p>
              {summary?.checked_at && (
                <p>
                  {t(($) => $['systemManage.casdoor.diagnosticChecked'], {
                    time: summary.checked_at,
                  })}
                </p>
              )}
              {summary?.expires_at && (
                <p>
                  {t(($) => $['systemManage.casdoor.diagnosticExpiry'], {
                    time: summary.expires_at,
                  })}
                </p>
              )}
              {summary?.correlation_id && (
                <p>
                  {t(($) => $['systemManage.casdoor.correlation'], { id: summary.correlation_id })}
                </p>
              )}
              {summary?.status === 'failed' && kind === 'diagnostic' && (
                <p>{t(($) => $['systemManage.casdoor.verificationHelp'])}</p>
              )}
              {summary?.code && <p>{summary.code}</p>}
            </div>
          )
        })}
      </div>
      {successfulPreview && (
        <section
          aria-label={t(($) => $['systemManage.casdoor.diagnosticPreview'])}
          className="space-y-2"
        >
          <h4 className="font-medium">{t(($) => $['systemManage.casdoor.diagnosticPreview'])}</h4>
          <p>{t(($) => $['systemManage.casdoor.diagnosticPreviewHelp'])}</p>
          <p>
            {t(($) => $['systemManage.casdoor.diagnosticRoleCount'], {
              count: preview.effective_role_count,
            })}
          </p>
          <ul className="space-y-1">
            {preview.stages.map((stage) => (
              <li key={stage.stage}>
                {t(($) => $['systemManage.casdoor.diagnosticCheck'], {
                  stage: stages[stage.stage],
                  status: statuses[stage.status],
                })}
                {stage.code && <> · {stage.code}</>}
              </li>
            ))}
          </ul>
          <ul className="space-y-1">
            {preview.targets.map((target) => (
              <li key={target.workspace_id}>
                {t(($) => $['systemManage.casdoor.diagnosticTarget'], {
                  id: target.workspace_id,
                  role: t(($) => $[`systemManage.casdoor.${target.target_role}`]),
                  reason:
                    target.reason === 'role_mapping'
                      ? t(($) => $['systemManage.casdoor.diagnosticMapping'])
                      : t(($) => $['systemManage.casdoor.diagnosticFallback']),
                })}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
