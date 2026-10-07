import type {
  CasdoorRevisionResponse,
  CasdoorValidationSummaryResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { useEffect, useState } from 'react'
import { useTranslation } from '#i18n'

function LoginTestOutcome({ summary }: { summary: CasdoorValidationSummaryResponse }) {
  const { t } = useTranslation('extend')
  const [now, setNow] = useState(Date.now)
  const deadline = summary.expires_at ? Date.parse(summary.expires_at) : Number.NaN
  const checked = summary.checked_at ? Date.parse(summary.checked_at) : Number.NaN
  let status = summary.status
  if (status === 'passed') {
    if (
      !Number.isFinite(deadline) ||
      !Number.isFinite(checked) ||
      checked > now ||
      checked >= deadline
    )
      status = 'unknown'
    else if (deadline <= now) status = 'expired'
  }

  useEffect(() => {
    if (status !== 'passed') return
    const timer = window.setTimeout(
      () => setNow(Date.now()),
      Math.min(Math.max(deadline - now, 0), 2_147_483_647),
    )
    return () => window.clearTimeout(timer)
  }, [deadline, now, status])

  const label = {
    passed: t(($) => $['systemManage.casdoor.diagnosticPassed']),
    failed: t(($) => $['systemManage.casdoor.diagnosticFailed']),
    expired: t(($) => $['systemManage.casdoor.diagnosticExpired']),
    unknown: t(($) => $['systemManage.casdoor.diagnosticUnknown']),
    not_run: t(($) => $['systemManage.casdoor.diagnosticNotRun']),
  }[status]
  return (
    <div className="space-y-1 text-sm text-text-secondary">
      <p role="status">
        {t(($) => $['systemManage.casdoor.diagnosticCheck'], {
          stage: t(($) => $['systemManage.casdoor.testLogin']),
          status: label,
        })}
      </p>
      {status === 'failed' && (
        <details>
          <summary className="cursor-pointer">
            {t(($) => $['systemManage.casdoor.diagnosticPreview'])}
          </summary>
          <p>{t(($) => $['systemManage.casdoor.loginRetryHelp'])}</p>
          <p>{t(($) => $['systemManage.casdoor.verificationHelp'])}</p>
          {summary.correlation_id && (
            <p>{t(($) => $['systemManage.casdoor.correlation'], { id: summary.correlation_id })}</p>
          )}
        </details>
      )}
    </div>
  )
}

export function LoginTestStatus({ revision }: { revision: CasdoorRevisionResponse }) {
  const summary = revision.validation?.find(
    (entry) => entry.kind === 'diagnostic' && entry.revision_id === revision.revision_id,
  )
  if (!summary) return null
  return (
    <LoginTestOutcome
      key={JSON.stringify([
        summary.revision_id,
        summary.correlation_id,
        summary.status,
        summary.checked_at,
        summary.expires_at,
      ])}
      summary={summary}
    />
  )
}
