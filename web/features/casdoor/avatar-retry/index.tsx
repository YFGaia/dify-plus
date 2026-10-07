'use client'

import { Button } from '@langgenius/dify-ui/button'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { useTranslation } from '#i18n'
import { captureManagerSource, useManagerSourceKey } from '../manager-source'
import { avatarRetryOptions, avatarRetryTargetsOptions } from './client'

function RetrySession() {
  const { t } = useTranslation('extend')
  const client = useQueryClient()
  const source = captureManagerSource(client)
  const id = useId()
  const [after, setAfter] = useState<string>()
  const [selected, setSelected] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [submitted, setSubmitted] = useState<string[]>([])
  const [outcome, setOutcome] = useState<'pending' | 'unknown' | null>(null)
  const inFlightRef = useRef<object | null>(null)
  const operationRef = useRef<object | null>(null)
  const targets = useQuery(
    avatarRetryTargetsOptions(client, after === undefined ? { limit: 20 } : { after, limit: 20 }),
  )
  const retry = useMutation(avatarRetryOptions(client))
  const current =
    targets.isSuccess && !targets.isFetching && !targets.isError ? targets.data : undefined
  const target = current?.targets.find((item) => item.intent_id === selected)
  const canSubmit = target && !submitted.includes(target.intent_id)
  const live = (handle: object) => {
    try {
      source.check()
      return operationRef.current === handle
    } catch {
      return false
    }
  }
  return (
    <section className="space-y-3" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="font-semibold">
        {t(($) => $['casdoorAvatarRetry.title'])}
      </h3>
      <p>{t(($) => $['casdoorAvatarRetry.description'])}</p>
      {targets.isError && <p role="alert">{t(($) => $['casdoorIdentity.actionIncomplete'])}</p>}
      {outcome && (
        <p role={outcome === 'unknown' ? 'alert' : 'status'}>
          {outcome === 'pending'
            ? t(($) => $['casdoorAvatarRetry.pending'])
            : t(($) => $['casdoorAvatarRetry.unknown'])}
        </p>
      )}
      <Button
        type="button"
        loading={targets.isFetching}
        disabled={retry.isPending}
        onClick={() => {
          operationRef.current = null
          setSelected('')
          setConfirmed(false)
          if (after !== undefined) setAfter(undefined)
          else void targets.refetch().catch(() => {})
        }}
      >
        {t(($) => $['systemManage.casdoor.refresh'])}
      </Button>
      {current && (
        <>
          <label htmlFor={`${id}-target`}>{t(($) => $['casdoorAvatarRetry.target'])}</label>
          <select
            id={`${id}-target`}
            value={selected}
            disabled={retry.isPending}
            className="block w-full rounded-lg border border-divider-regular p-2 focus-visible:outline-2 focus-visible:outline-components-button-primary-bg"
            onChange={(event) => {
              operationRef.current = null
              setSelected(event.target.value)
              setConfirmed(false)
            }}
          >
            <option value="">{t(($) => $['casdoorAvatarRetry.target'])}</option>
            {current.targets.map((item) => (
              <option key={item.intent_id} value={item.intent_id}>
                {item.account_id} / {item.identity_id} / {item.intent_id}
              </option>
            ))}
          </select>
          {!current.targets.length && <p>{t(($) => $['casdoorIdentity.empty'])}</p>}
          {current.has_more && current.next_after && (
            <Button
              type="button"
              disabled={retry.isPending}
              onClick={() => {
                operationRef.current = null
                setSelected('')
                setConfirmed(false)
                setAfter(current.next_after ?? undefined)
              }}
            >
              {t(($) => $['casdoorAvatarRetry.nextPage'])}
            </Button>
          )}
        </>
      )}
      {canSubmit && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!confirmed || retry.isPending || inFlightRef.current) return
            const handle = {}
            operationRef.current = handle
            inFlightRef.current = handle
            setSubmitted((previous) => [...previous, target.intent_id])
            setConfirmed(false)
            retry.mutate(
              { body: { intent_id: target.intent_id } },
              {
                onSuccess: () => {
                  if (live(handle)) setOutcome('pending')
                },
                onError: () => {
                  if (live(handle)) setOutcome('unknown')
                },
                onSettled: () => {
                  if (inFlightRef.current === handle) inFlightRef.current = null
                  retry.reset()
                },
              },
            )
          }}
        >
          <p>
            {target.reason === 'fetch_rejected'
              ? t(($) => $['casdoorAvatarRetry.fetchRejected'])
              : target.reason === 'fetch_failed'
                ? t(($) => $['casdoorAvatarRetry.fetchFailed'])
                : t(($) => $['casdoorAvatarRetry.imageRejected'])}
          </p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {t(($) => $['casdoorLocal.confirm'])}
          </label>
          <Button type="submit" disabled={!confirmed} loading={retry.isPending}>
            {t(($) => $['systemManage.casdoor.confirm'])}
          </Button>
        </form>
      )}
    </section>
  )
}

export function AvatarRetry() {
  const client = useQueryClient()
  const key = useManagerSourceKey(client)
  return key === null ? null : <RetrySession key={key} />
}
