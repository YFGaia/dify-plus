'use client'

import { Button, buttonVariants } from '@langgenius/dify-ui/button'
import { QueryClient, useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from '#i18n'
import { useSearchParams } from '@/next/navigation'
import { consoleQuery } from '@/service/console'
import { basePath } from '@/utils/var'
import { parseRPLogoutContinuation } from './navigation'

export function CasdoorLogoutResult() {
  const { t } = useTranslation('extend')
  const search = useSearchParams()
  const returned = search.get('status') === 'returned'
  const [client] = useState(() => new QueryClient())
  const [unavailable, setUnavailable] = useState(false)
  const retry = useMutation(
    consoleQuery.auth.casdoor.logout.retry.post.mutationOptions({
      retry: false,
      gcTime: 0,
      networkMode: 'always',
      context: { silent: true },
      onSuccess(data) {
        try {
          const destination = parseRPLogoutContinuation(data)
          if (destination) window.location.assign(destination)
          else setUnavailable(true)
        } catch {
          setUnavailable(true)
        }
      },
      onError() {
        setUnavailable(true)
      },
    }),
    client,
  )
  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-semibold text-text-primary">
        {t(($) => $['casdoorLogout.title'])}
      </h1>
      <p role="status" className="text-text-secondary">
        {returned
          ? t(($) => $['casdoorLogout.returned'])
          : t(($) => $['casdoorLogout.unavailable'])}
      </p>
      {!returned && (
        <Button
          type="button"
          disabled={retry.isPending || unavailable}
          onClick={() => retry.mutate({})}
        >
          {t(($) => $['casdoorLogout.retry'])}
        </Button>
      )}
      {unavailable && <p role="alert">{t(($) => $['casdoorLogout.retryUnavailable'])}</p>}
      <a
        className={buttonVariants({ variant: 'secondary', size: 'large' })}
        href={`${basePath}/signin`}
      >
        {t(($) => $['casdoorSigninResult.freshSignin'])}
      </a>
    </section>
  )
}
