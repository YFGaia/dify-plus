'use client'

import { buttonVariants } from '@langgenius/dify-ui/button'
import { QueryClient, useMutation } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { basePath } from '@/utils/var'
import { parseSigninResult } from './client'

const resultPath = `${basePath}/signin/casdoor-result`

function readHandoff() {
  if (window.location.pathname !== resultPath) return undefined
  const entries = [...new URLSearchParams(window.location.search)]
  if (entries.length !== 1) return undefined
  const entry = entries[0]
  if (!entry || entry[0] !== 'handoff' || !/^[\w-]{42}[AEIMQUYcgkosw048]$/.test(entry[1]))
    return undefined
  return entry[1]
}

export function CasdoorSigninResult() {
  const { t } = useTranslation('extend')
  const [queryClient] = useState(() => new QueryClient())
  const [invalidHandoff, setInvalidHandoff] = useState(false)
  const startedRef = useRef(false)
  const ownerRef = useRef<{ alive: boolean; handoff?: string } | null>(null)
  const mutation = useMutation(
    consoleQuery.auth.casdoor.result.get.mutationOptions({
      retry: false,
      networkMode: 'always',
      gcTime: 0,
      context: {
        silent: true,
        beforeCasdoorResultRequest: () => {
          const current = ownerRef.current
          if (
            !startedRef.current ||
            !current?.alive ||
            !current.handoff ||
            readHandoff() !== current.handoff
          )
            throw new Error('Sign-in result request is no longer available.')
          // The native adapter dispatches immediately after this synchronous callback.
          window.history.replaceState(null, '', resultPath)
        },
      },
    }),
    queryClient,
  )
  const { mutate } = mutation

  useEffect(() => {
    const epoch = { alive: true, handoff: readHandoff() }
    ownerRef.current = epoch
    // This URL is a one-use browser command. StrictMode's first setup is invalidated
    // before its microtask; cleanup never aborts or restarts an already sent command.
    queueMicrotask(() => {
      if (!epoch.alive || ownerRef.current !== epoch || startedRef.current) return
      if (!epoch.handoff) {
        setInvalidHandoff(true)
        return
      }
      startedRef.current = true
      mutate({ query: { handoff: epoch.handoff } })
    })
    return () => {
      epoch.alive = false
    }
  }, [mutate])

  const result = parseSigninResult(mutation.data)
  const loading = !invalidHandoff && (mutation.isIdle || mutation.isPending)
  const message = invalidHandoff
    ? t(($) => $['casdoorSigninResult.expired'])
    : result?.code === 'role_snapshot_unknown'
      ? t(($) => $['casdoorSigninResult.unknownRole'])
      : result?.code === 'workspace_unavailable'
        ? t(($) => $['casdoorSigninResult.workspaceUnavailable'])
        : result?.code === 'authorization_pending'
          ? t(($) => $['casdoorSigninResult.pending'])
          : t(($) => $['casdoorSigninResult.generic'])

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-text-primary">
        {t(($) => $['casdoorSigninResult.title'])}
      </h1>
      {loading ? (
        <div role="status" className="space-y-3 text-text-secondary">
          <p>{t(($) => $['casdoorSigninResult.loading'])}</p>
          <div aria-hidden="true" className="h-12 animate-pulse rounded-lg bg-background-section" />
        </div>
      ) : (
        <div role="alert" className="space-y-3 text-text-secondary">
          <p>{message}</p>
          {result && (
            <p className="text-sm wrap-break-word">
              {t(($) => $['casdoorSigninResult.correlation'], { id: result.correlation_id })}
            </p>
          )}
        </div>
      )}
      <a
        className={buttonVariants({ variant: 'primary', size: 'large' })}
        href={`${basePath}/signin`}
      >
        {t(($) => $['casdoorSigninResult.freshSignin'])}
      </a>
    </div>
  )
}
