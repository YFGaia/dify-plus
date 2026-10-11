'use client'

import type { consoleClient } from '@/service/console'
import { Button } from '@langgenius/dify-ui/button'
import { useMutation } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { consoleQuery } from '@/service/console'

type Registration = Awaited<
  ReturnType<typeof consoleClient.systemManageExtend.integration.dingtalk.registration.start.post>
>
type RegistrationResult = Awaited<
  ReturnType<typeof consoleClient.systemManageExtend.integration.dingtalk.registration.poll.post>
>

export default function DingTalkRegistration({ onComplete }: { onComplete: () => Promise<void> }) {
  const { t } = useTranslation('extend')
  const operation = consoleQuery.systemManageExtend.integration.dingtalk.registration
  const start = useMutation(operation.start.post.mutationOptions({ context: { silent: true } }))
  const poll = useMutation(operation.poll.post.mutationOptions({ context: { silent: true } }))
  const cancel = useMutation(
    operation.byTransactionId.delete.mutationOptions({ context: { silent: true } }),
  )
  const [registration, setRegistration] = useState<Registration | null>(null)
  const [result, setResult] = useState<RegistrationResult | null>(null)
  const [remaining, setRemaining] = useState(0)
  const [error, setError] = useState('')
  const { mutateAsync: pollTransaction } = poll
  const waiting = result?.status === 'WAITING'

  // The server owns the transaction and secrets; this timer only schedules its requested polling interval.
  useEffect(() => {
    if (!registration || !waiting || error || typeof registration.interval !== 'number') return
    let active = true
    const timer = window.setTimeout(
      async () => {
        try {
          const response = await pollTransaction({
            body: { transaction_id: registration.transaction_id },
          })
          if (!active) return
          setResult(response)
          if (response.status === 'SUCCESS') await onComplete()
        } catch {
          if (active) setError(t(($) => $['systemManage.dingtalk.registrationFailed']))
        }
      },
      Math.max(result?.interval ?? registration.interval, 1) * 1000,
    )
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [registration, waiting, result, error, pollTransaction, onComplete, t])

  useEffect(() => {
    if (!waiting) return
    const timer = window.setInterval(
      () => setRemaining((seconds) => Math.max(0, seconds - 1)),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [waiting])

  const handleStart = async () => {
    setError('')
    try {
      const response = await start.mutateAsync({})
      if (
        typeof response.verification_uri_complete !== 'string' ||
        !response.verification_uri_complete.startsWith('https://') ||
        typeof response.interval !== 'number' ||
        typeof response.expires_in !== 'number' ||
        response.status !== 'WAITING'
      )
        throw new Error('invalid_provider_response')
      setRegistration(response)
      setResult({ transaction_id: response.transaction_id, status: response.status })
      setRemaining(response.expires_in)
    } catch {
      setError(t(($) => $['systemManage.dingtalk.registrationFailed']))
    }
  }
  const handleCancel = async () => {
    if (!registration) return
    setError('')
    try {
      const response = await cancel.mutateAsync({
        params: { transaction_id: registration.transaction_id },
      })
      setResult(response)
    } catch {
      setError(t(($) => $['systemManage.dingtalk.registrationFailed']))
    }
  }

  return (
    <section
      className="space-y-3 rounded-xl border border-divider-subtle p-4"
      aria-labelledby="dingtalk-qr-title"
    >
      <h2 id="dingtalk-qr-title" className="text-sm font-semibold text-text-primary">
        {t(($) => $['systemManage.dingtalk.qrSetup'])}
      </h2>
      <p className="text-sm text-text-secondary">{t(($) => $['systemManage.dingtalk.qrHint'])}</p>
      {waiting && registration && (
        <>
          <QRCodeSVG
            value={registration.verification_uri_complete}
            size={200}
            title={t(($) => $['systemManage.dingtalk.qrSetup'])}
            className="rounded-lg bg-white p-2"
          />
          <p className="text-sm text-text-secondary">
            {t(($) => $['systemManage.dingtalk.expiresIn'], { seconds: remaining })}
          </p>
        </>
      )}
      {result && (
        <p role="status" className="text-sm text-text-secondary">
          {t(($) => $[`systemManage.dingtalk.registration.${result.status}`])}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-3">
        {!waiting && (
          <Button type="button" variant="primary" loading={start.isPending} onClick={handleStart}>
            {t(($) => $['systemManage.dingtalk.createQr'])}
          </Button>
        )}
        {waiting && (
          <Button type="button" loading={cancel.isPending} onClick={handleCancel}>
            {t(($) => $['systemManage.common.cancel'])}
          </Button>
        )}
        {waiting && error && (
          <Button type="button" onClick={() => setError('')}>
            {t(($) => $['operation.retry'], { ns: 'common' })}
          </Button>
        )}
      </div>
    </section>
  )
}
