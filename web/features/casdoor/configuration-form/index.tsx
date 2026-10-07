'use client'

import { Button } from '@langgenius/dify-ui/button'
import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useTranslation } from '#i18n'
import { useCasdoorManagementAccess } from '@/features/casdoor/management-access/use-casdoor-management-access'
import { consoleQuery } from '@/service/console'
import { parseServerConfiguration } from './configuration-draft'
import { ConfigurationSession } from './configuration-session'
import { ManagementError } from './management-error'

export function CasdoorConfigurationForm() {
  const { t } = useTranslation('extend')
  const {
    canManageCasdoor: canManage,
    isPending: permissionPending,
    error: permissionError,
    refetch: refetchPermission,
  } = useCasdoorManagementAccess()
  const configuration = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.get.queryOptions({
      context: { silent: true },
      enabled: canManage,
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
  const refetch = configuration.refetch
  useEffect(() => {
    if (!canManage) return
    const refreshOnReturn = (event: PageTransitionEvent) => {
      if (event.persisted) void refetch()
    }
    window.addEventListener('pageshow', refreshOnReturn)
    return () => window.removeEventListener('pageshow', refreshOnReturn)
  }, [canManage, refetch])
  if (permissionError)
    return (
      <>
        <ManagementError error={permissionError} />
        <Button
          type="button"
          onClick={() => {
            void refetchPermission()
          }}
        >
          {t(($) => $['systemManage.casdoor.retry'])}
        </Button>
      </>
    )
  if (!canManage && !permissionPending)
    return <p role="alert">{t(($) => $['systemManage.common.noPermission'])}</p>
  if (permissionPending || configuration.isPending)
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
      <ConfigurationSession
        response={configuration.data}
        onRefresh={async () => {
          const current = await configuration.refetch()
          return current.isSuccess ? current.data : null
        }}
        refreshing={configuration.isFetching}
        refreshError={configuration.error}
      />
    </section>
  )
}
