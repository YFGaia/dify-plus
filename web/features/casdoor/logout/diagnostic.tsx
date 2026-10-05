'use client'

import type { CasdoorRevisionResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { zCasdoorRpLogoutDiagnosticResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { Button } from '@langgenius/dify-ui/button'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from '#i18n'
import { parseDiagnosticStart } from '@/features/casdoor/configuration-form/diagnostic-navigation'
import { consoleQuery } from '@/service/console'

export function RPLogoutDiagnostic({
  revision,
  etag,
}: {
  revision: CasdoorRevisionResponse
  etag: number
}) {
  const { t } = useTranslation('extend')
  const [confirmed, setConfirmed] = useState(false)
  const [failed, setFailed] = useState(false)
  const [now, setNow] = useState(Date.now)
  const status = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.rpLogoutStatus.get.queryOptions({
      input: { query: { revision_id: revision.revision_id } },
      enabled: revision.configuration.rp_logout === true,
      staleTime: 0,
      gcTime: 0,
      retry: false,
      refetchOnWindowFocus: 'always',
      context: { silent: true },
      select(data) {
        const result = zCasdoorRpLogoutDiagnosticResponse.strict().parse(data)
        if (
          result.revision_id !== revision.revision_id ||
          result.namespace_id !== revision.namespace_id
        )
          throw new Error('Logout diagnostic unavailable.')
        return result
      },
    }),
  )
  const refetch = status.refetch
  const observedNow = Math.max(now, status.dataUpdatedAt)
  const checked = status.data?.checked_at ? Date.parse(status.data.checked_at) : Number.NaN
  const expires = status.data?.expires_at ? Date.parse(status.data.expires_at) : Number.NaN
  const fresh =
    status.data?.status === 'passed' &&
    status.data.profile_available &&
    Number.isFinite(checked) &&
    Number.isFinite(expires) &&
    checked <= observedNow &&
    observedNow < expires &&
    checked < expires &&
    expires - checked <= 300_000
  useEffect(() => {
    if (!Number.isFinite(expires) || expires <= observedNow) return
    const timer = window.setTimeout(
      () => {
        setNow(Date.now())
        void refetch()
      },
      Math.min(Math.max(expires - Date.now(), 0), 2_147_483_647),
    )
    return () => window.clearTimeout(timer)
  }, [expires, observedNow, refetch])
  const start = useMutation(
    consoleQuery.systemManageExtend.integration.casdoor.testRpLogout.post.mutationOptions({
      retry: false,
      gcTime: 0,
      networkMode: 'always',
      context: { silent: true },
      onSuccess(data) {
        try {
          const result = parseDiagnosticStart(data)
          if (result.destination) window.location.assign(result.destination)
          else setFailed(true)
        } catch {
          setFailed(true)
        }
      },
      onError() {
        setFailed(true)
      },
    }),
  )
  if (!revision.configuration.rp_logout) return null
  return (
    <section className="space-y-3 rounded-lg border border-divider-subtle p-4">
      <h3 className="font-semibold">{t(($) => $['casdoorLogout.title'])}</h3>
      <p className="text-sm text-text-secondary">{t(($) => $['casdoorLogout.diagnosticHelp'])}</p>
      <p role="status">
        {fresh
          ? t(($) => $['casdoorLogout.returned'])
          : status.data?.status === 'passed' && expires <= observedNow
            ? t(($) => $['systemManage.casdoor.diagnosticExpired'])
            : t(($) => $['casdoorLogout.unavailable'])}
      </p>
      {!status.data?.profile_available && <p>{t(($) => $['casdoorLogout.notReviewed'])}</p>}
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>{t(($) => $['casdoorLogout.browserWarning'])}</span>
      </label>
      <Button
        type="button"
        disabled={!confirmed || !status.data?.profile_available || start.isPending}
        onClick={() => {
          setFailed(false)
          start.mutate({ body: { revision_id: revision.revision_id, etag } })
        }}
      >
        {t(($) => $['casdoorLogout.test'])}
      </Button>
      {(failed || status.isError) && (
        <p role="alert">{t(($) => $['casdoorLogout.retryUnavailable'])}</p>
      )}
    </section>
  )
}
