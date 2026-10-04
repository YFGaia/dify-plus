import { useTranslation } from '#i18n'
import { safeManagementError } from './management-error-details'

export function ManagementError({ error }: { error: unknown }) {
  const { t } = useTranslation('extend')
  const safe = safeManagementError(error)
  return (
    <div role="alert" className="space-y-1 text-text-destructive">
      <p>
        {safe.unauthorized
          ? t(($) => $['systemManage.casdoor.unauthorized'])
          : t(($) => $['systemManage.casdoor.requestFailed'])}
      </p>
      {safe.code && <p>{safe.code}</p>}
      {safe.code === 'config_conflict' && <p>{t(($) => $['systemManage.casdoor.conflict'])}</p>}
      {safe.reason === 'deployment_proof_missing' && (
        <p>{t(($) => $['systemManage.casdoor.deploymentProofMissing'])}</p>
      )}
      {safe.reason === 'live_test_not_wired' && (
        <p>{t(($) => $['systemManage.casdoor.liveTestNotWired'])}</p>
      )}
      {safe.correlationId && (
        <p>{t(($) => $['systemManage.casdoor.correlation'], { id: safe.correlationId })}</p>
      )}
    </div>
  )
}
