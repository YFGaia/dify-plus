'use client'

import { zCasdoorPermissionsResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { Button } from '@langgenius/dify-ui/button'
import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { AvatarRetry } from '../avatar-retry'
import { LocalLifecycle } from '../local-lifecycle'
import { RPLogoutDiagnostic } from '../logout/diagnostic'
import { NamespaceReset } from '../namespace-reset'
import { parseServerConfiguration } from './configuration-draft'
import { ConfigurationSession } from './configuration-session'
import { ManagementError } from './management-error'

export function CasdoorConfigurationForm() {
  const { t } = useTranslation('extend')
  const permissions = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.permissions.get.queryOptions({
      context: { silent: true },
      retry: false,
      select: (data) => {
        const parsed = zCasdoorPermissionsResponse.safeParse(data)
        if (!parsed.success) throw new Error('Casdoor permission response unavailable.')
        return parsed.data
      },
    }),
  )
  const configuration = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.get.queryOptions({
      context: { silent: true },
      enabled: permissions.data?.can_manage_casdoor === true,
      retry: false,
      staleTime: 0,
      refetchOnMount: 'always',
      refetchOnWindowFocus: 'always',
      select: (data) => {
        const parsed = parseServerConfiguration(data)
        if (!parsed) throw new Error('Casdoor configuration response unavailable.')
        return parsed
      },
    }),
  )
  const canManage = permissions.data?.can_manage_casdoor === true
  const refetch = configuration.refetch
  useEffect(() => {
    if (!canManage) return
    const refreshOnReturn = (event: PageTransitionEvent) => {
      if (event.persisted) void refetch()
    }
    window.addEventListener('pageshow', refreshOnReturn)
    return () => window.removeEventListener('pageshow', refreshOnReturn)
  }, [canManage, refetch])
  if (permissions.isError)
    return (
      <>
        <ManagementError error={permissions.error} />
        <Button
          type="button"
          onClick={() => {
            void permissions.refetch()
          }}
        >
          {t(($) => $['systemManage.casdoor.retry'])}
        </Button>
      </>
    )
  if (permissions.data && !permissions.data.can_manage_casdoor)
    return <p role="alert">{t(($) => $['systemManage.casdoor.unauthorized'])}</p>
  if (permissions.isPending || configuration.isPending)
    return (
      <div role="status" className="space-y-3">
        <p>{t(($) => $['systemManage.casdoor.loading'])}</p>
        <div aria-hidden="true" className="h-32 animate-pulse rounded-lg bg-background-section" />
        <div aria-hidden="true" className="h-64 animate-pulse rounded-lg bg-background-section" />
      </div>
    )
  if (!configuration.data)
    return (
      <>
        <p role="alert">{t(($) => $['systemManage.casdoor.loadFailed'])}</p>
        {configuration.error && <ManagementError error={configuration.error} />}
        <Button
          type="button"
          onClick={() => {
            void configuration.refetch()
          }}
        >
          {t(($) => $['systemManage.casdoor.retry'])}
        </Button>
      </>
    )
  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold">{t(($) => $['systemManage.casdoor.title'])}</h2>
      <p className="text-sm text-text-secondary">
        {t(($) => $['systemManage.casdoor.description'])}
      </p>
      <ConfigurationSession
        response={configuration.data}
        onRefresh={() => {
          void configuration.refetch()
        }}
        refreshing={configuration.isFetching}
        refreshError={configuration.error}
      />
      <LocalLifecycle />
      <NamespaceReset
        response={configuration.data}
        refreshing={configuration.isFetching}
        refreshError={configuration.error}
        onRefresh={async () => {
          const current = await configuration.refetch()
          return current.isSuccess ? current.data : null
        }}
      />
      <AvatarRetry />
      {configuration.data.draft && (
        <RPLogoutDiagnostic
          key={`${configuration.data.draft.revision_id}:${configuration.data.etag}`}
          revision={configuration.data.draft}
          etag={configuration.data.etag}
        />
      )}
    </section>
  )
}
