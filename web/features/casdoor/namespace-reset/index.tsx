'use client'

import type {
  CasdoorConfigurationResponse,
  CasdoorNamespaceResetReviewResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { Button } from '@langgenius/dify-ui/button'
import { hashKey, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { captureManagerSource, useManagerSourceKey } from '../manager-source'
import { namespaceResetOptions, resetContext, resetReviewOptions } from './client'

type Props = {
  response: CasdoorConfigurationResponse
  refreshing: boolean
  refreshError: unknown
  onRefresh: () => Promise<CasdoorConfigurationResponse | null>
}

function ResetSession({ response, refreshing, refreshError, onRefresh }: Props) {
  const { t } = useTranslation('extend')
  const client = useQueryClient()
  const source = captureManagerSource(client)
  const id = useId()
  const context = resetContext(response)
  const reviewEpochRef = useRef(0)
  const inFlightRef = useRef<object | null>(null)
  const [consent, setConsent] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [reviewed, setReviewed] = useState<{
    data: CasdoorNamespaceResetReviewResponse
    deadline: number
    version: number
  } | null>(null)
  const [expired, setExpired] = useState(false)
  const [outcome, setOutcome] = useState<'received' | 'current' | 'unknown' | null>(null)
  const configKey = consoleQuery.systemManageExtend.integration.casdoor.get.queryKey()
  const configHash = hashKey(configKey)
  const version = useSyncExternalStore(
    (notify) =>
      client.getQueryCache().subscribe((event) => {
        if (event.query.queryHash !== configHash) return
        if (
          event.type === 'removed' ||
          (event.type === 'updated' && event.action.type === 'success') ||
          event.query.state.status !== 'success' ||
          event.query.state.fetchStatus !== 'idle'
        ) {
          reviewEpochRef.current++
          setReviewed(null)
          setConfirmed(false)
          setConsent(false)
        }
        notify()
      }),
    () => client.getQueryState(configKey)?.dataUpdateCount ?? 0,
    () => 0,
  )
  const review = useMutation(resetReviewOptions(client))
  const reset = useMutation(namespaceResetOptions(client, context?.namespace_id ?? null))
  const operationRef = useRef<object | null>(null)
  const busy = review.isPending || reset.isPending
  const available = context !== null && !refreshing && !refreshError
  const currentReview =
    available &&
    reviewed?.version === version &&
    reviewed.data.namespace_id === context.namespace_id &&
    reviewed.data.etag === context.etag &&
    !expired
      ? reviewed
      : null
  useEffect(() => {
    if (!reviewed) return
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, reviewed.deadline - performance.now()),
    )
    return () => window.clearTimeout(timer)
  }, [reviewed])
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
        {t(($) => $['casdoorReset.title'])}
      </h3>
      <p>{t(($) => $['casdoorReset.description'])}</p>
      {outcome && (
        <p role={outcome === 'unknown' ? 'alert' : 'status'}>
          {outcome === 'unknown'
            ? t(($) => $['casdoorReset.unknown'])
            : outcome === 'current'
              ? t(($) => $['casdoorReset.current'])
              : t(($) => $['casdoorReset.received'])}
        </p>
      )}
      <Button
        type="button"
        loading={refreshing}
        disabled={busy}
        onClick={() => {
          operationRef.current = null
          setReviewed(null)
          setConfirmed(false)
          setConsent(false)
          void onRefresh().catch(() => {})
        }}
      >
        {t(($) => $['systemManage.casdoor.refresh'])}
      </Button>
      {available && outcome !== 'unknown' && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!context || !consent || busy || inFlightRef.current) return
            const handle = {}
            operationRef.current = handle
            inFlightRef.current = handle
            setReviewed(null)
            setConfirmed(false)
            setExpired(false)
            setOutcome(null)
            const epoch = reviewEpochRef.current
            review.mutate(
              { body: { ...context, confirm_management_review: true } },
              {
                onSuccess: (data) => {
                  if (live(handle) && reviewEpochRef.current === epoch)
                    setReviewed({
                      data,
                      deadline: performance.now() + data.expires_in * 1000,
                      version,
                    })
                },
                onError: () => {
                  if (live(handle)) setOutcome('unknown')
                },
                onSettled: () => {
                  if (inFlightRef.current === handle) inFlightRef.current = null
                  review.reset()
                },
              },
            )
          }}
        >
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={consent}
              disabled={busy}
              onChange={(event) => setConsent(event.target.checked)}
            />
            {t(($) => $['casdoorReset.reviewConsent'])}
          </label>
          <Button type="submit" loading={review.isPending} disabled={!consent || reset.isPending}>
            {t(($) => $['casdoorReset.review'])}
          </Button>
        </form>
      )}
      {currentReview && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (inFlightRef.current) return
            if (!confirmed || busy || performance.now() >= currentReview.deadline) {
              setExpired(true)
              return
            }
            const handle = {}
            operationRef.current = handle
            inFlightRef.current = handle
            const data = currentReview.data
            setReviewed(null)
            setConfirmed(false)
            setConsent(false)
            reset.mutate(
              { body: { review_id: data.review_id, etag: data.etag } },
              {
                onSuccess: (result) => {
                  if (!live(handle)) return
                  setOutcome('received')
                  void onRefresh()
                    .then((fresh) => {
                      if (!live(handle)) return
                      const readback = resetContext(fresh)
                      if (
                        readback &&
                        fresh?.draft &&
                        fresh.active === null &&
                        fresh.active_revision_id === null &&
                        readback.etag === result.etag &&
                        fresh.draft_revision_id === result.draft_revision_id &&
                        fresh.draft.namespace_id === result.draft?.namespace_id
                      )
                        setOutcome('current')
                      else setOutcome('unknown')
                    })
                    .catch(() => {
                      if (live(handle)) setOutcome('unknown')
                    })
                },
                onError: () => {
                  if (live(handle)) setOutcome('unknown')
                },
                onSettled: () => {
                  if (inFlightRef.current === handle) inFlightRef.current = null
                  reset.reset()
                },
              },
            )
          }}
        >
          <p id={`${id}-format`}>{t(($) => $['casdoorReset.formatOnly'])}</p>
          <p>{t(($) => $['casdoorLocal.expires'])}</p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              aria-describedby={`${id}-format`}
            />
            {t(($) => $['casdoorLocal.confirm'])}
          </label>
          <Button type="submit" disabled={!confirmed} loading={reset.isPending}>
            {t(($) => $['systemManage.casdoor.confirm'])}
          </Button>
        </form>
      )}
    </section>
  )
}

export function NamespaceReset(props: Props) {
  const client = useQueryClient()
  const key = useManagerSourceKey(client)
  return key === null ? null : <ResetSession key={key} {...props} />
}
