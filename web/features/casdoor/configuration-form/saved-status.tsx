import type { CasdoorConfigurationResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { useTranslation } from '#i18n'
import { DiagnosticStatus } from './diagnostic-status'

export function SavedStatus({
  response,
  onExpire,
}: {
  response: CasdoorConfigurationResponse
  onExpire: () => void
}) {
  const { t } = useTranslation('extend')
  return (
    <section className="space-y-1 rounded-lg border border-divider-regular p-3">
      <h3 className="font-medium">{t(($) => $['systemManage.casdoor.status'])}</h3>
      <p>
        {response.enabled
          ? t(($) => $['systemManage.casdoor.enabled'])
          : t(($) => $['systemManage.casdoor.disabled'])}
      </p>
      <details>
        <summary className="cursor-pointer text-sm text-text-secondary">
          {t(($) => $['systemManage.casdoor.details'])}
        </summary>
        <p>
          {t(($) => $['systemManage.casdoor.activeRevision'], {
            id: response.active_revision_id ?? t(($) => $['systemManage.casdoor.noRevision']),
          })}
        </p>
        <p>
          {t(($) => $['systemManage.casdoor.draftRevision'], {
            id: response.draft_revision_id ?? t(($) => $['systemManage.casdoor.noRevision']),
          })}
        </p>
        <p>{t(($) => $['systemManage.casdoor.etag'], { etag: response.etag })}</p>
        <p>
          {response.draft?.secret_configured
            ? t(($) => $['systemManage.casdoor.secretConfigured'])
            : t(($) => $['systemManage.casdoor.secretMissing'])}
        </p>
      </details>
      {response.draft && <DiagnosticStatus revision={response.draft} onExpire={onExpire} />}
    </section>
  )
}
