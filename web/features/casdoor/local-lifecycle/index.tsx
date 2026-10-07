'use client'

import type { CasdoorLocalMembershipReviewResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { Button } from '@langgenius/dify-ui/button'
import {
  hashKey,
  notifyManager,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useEffect, useId, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { identitySourceGuard } from '../identity-status/action-client'
import {
  localInspectionOptions,
  localMutationOptions,
  localReviewOptions,
  localTargetsOptions,
} from './client'

function MembershipReview({
  identityId,
  workspaceId,
}: {
  identityId: string
  workspaceId: string
}) {
  const { t } = useTranslation('extend')
  const client = useQueryClient()
  const id = useId()
  const inspection = useQuery(localInspectionOptions(client, identityId, workspaceId))
  const review = useMutation(localReviewOptions(client))
  const release = useMutation(localMutationOptions(client, 'release'))
  const adopt = useMutation(localMutationOptions(client, 'adopt'))
  const [reviewed, setReviewed] = useState<CasdoorLocalMembershipReviewResponse | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [result, setResult] = useState<'released' | 'adopted' | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!reviewed) return
    const timer = window.setTimeout(() => {
      setReviewed(null)
      setConfirmed(false)
    }, reviewed.expires_in * 1000)
    return () => window.clearTimeout(timer)
  }, [reviewed])
  const busy = review.isPending || release.isPending || adopt.isPending
  const current = inspection.isSuccess && !inspection.isFetching ? inspection.data : undefined
  const roleLabel = (role: CasdoorLocalMembershipReviewResponse['current_role']) =>
    role === 'admin'
      ? t(($) => $['systemManage.casdoor.admin'])
      : role === 'editor'
        ? t(($) => $['systemManage.casdoor.editor'])
        : t(($) => $['systemManage.casdoor.normal'])
  const prepare = (operation: 'release' | 'adopt') => {
    if (!current || busy) return
    setReviewed(null)
    setConfirmed(false)
    setFailed(false)
    setResult(null)
    review.mutate(
      {
        body: { identity_id: identityId, workspace_id: workspaceId, etag: current.etag, operation },
      },
      {
        onSuccess: setReviewed,
        onError: () => setFailed(true),
      },
    )
  }
  return (
    <div className="space-y-3">
      {(failed || inspection.isError) && (
        <p role="alert">{t(($) => $['casdoorIdentity.actionIncomplete'])}</p>
      )}
      {result && (
        <p role="status">
          {result === 'released'
            ? t(($) => $['casdoorLocal.released'])
            : t(($) => $['casdoorLocal.adopted'])}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          disabled={busy}
          loading={inspection.isFetching}
          onClick={() => {
            setReviewed(null)
            setConfirmed(false)
            void inspection.refetch().catch(() => {})
          }}
        >
          {t(($) => $['casdoorIdentity.current'])} · {t(($) => $['systemManage.casdoor.refresh'])}
        </Button>
        <Button
          type="button"
          disabled={busy || !current || !['managed', 'local_override'].includes(current.ownership)}
          onClick={() => prepare('release')}
        >
          {t(($) => $['casdoorLocal.release'])}
        </Button>
        <Button
          type="button"
          disabled={busy || !current || current.ownership === 'managed'}
          onClick={() => prepare('adopt')}
        >
          {t(($) => $['casdoorLocal.adopt'])}
        </Button>
      </div>
      {reviewed && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!confirmed || busy || !current || current.etag !== reviewed.etag) return
            const mutation = reviewed.operation === 'release' ? release : adopt
            mutation.mutate(
              { body: { review_id: reviewed.review_id, etag: reviewed.etag } },
              {
                onSuccess: (data) => setResult(data.status),
                onError: () => setFailed(true),
                onSettled: () => {
                  setReviewed(null)
                  setConfirmed(false)
                },
              },
            )
          }}
        >
          <p id={`${id}-review`}>
            {t(($) => $['casdoorLocal.reviewedRole'], {
              current: roleLabel(reviewed.current_role),
              target: roleLabel(reviewed.target_role),
            })}
          </p>
          <p>{t(($) => $['casdoorLocal.expires'])}</p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              aria-describedby={`${id}-review`}
            />
            {t(($) => $['casdoorLocal.confirm'])}
          </label>
          <Button
            type="submit"
            disabled={!confirmed || busy || !current || current.etag !== reviewed.etag}
            loading={release.isPending || adopt.isPending}
          >
            {t(($) => $['systemManage.casdoor.confirm'])}
          </Button>
        </form>
      )}
    </div>
  )
}

export function LocalLifecycle() {
  const { t } = useTranslation('extend')
  const client = useQueryClient()
  const [anchor] = useState(() => identitySourceGuard(client).anchor)
  const profileHash = hashKey(userProfileQueryOptions().queryKey)
  const hasSource = useSyncExternalStore(
    (notify) =>
      client.getQueryCache().subscribe(
        notifyManager.batchCalls((event) => {
          if (event.query.queryHash === profileHash) notify()
        }),
      ),
    () => typeof anchor === 'string' && identitySourceGuard(client).anchor === anchor,
    () => false,
  )
  const [cursor, setCursor] = useState<
    { after_identity_id: string; after_workspace_id: string } | undefined
  >()
  const [selected, setSelected] = useState('')
  const options = localTargetsOptions(client, cursor)
  const targets = useQuery({ ...options, enabled: hasSource && options.enabled })
  const target =
    hasSource && targets.isSuccess
      ? targets.data.items.find((item) => `${item.identity_id}/${item.workspace_id}` === selected)
      : undefined
  const id = useId()
  return (
    <section className="space-y-3" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="font-semibold">
        {t(($) => $['casdoorLocal.title'])}
      </h3>
      <p className="text-sm text-text-secondary">{t(($) => $['casdoorLocal.description'])}</p>
      {targets.isError && <p role="alert">{t(($) => $['casdoorIdentity.actionIncomplete'])}</p>}
      <Button
        type="button"
        disabled={!hasSource}
        loading={targets.isFetching}
        onClick={() => {
          setSelected('')
          if (cursor) setCursor(undefined)
          else void targets.refetch().catch(() => {})
        }}
      >
        {t(($) => $['casdoorLocal.target'])} · {t(($) => $['systemManage.casdoor.refresh'])}
      </Button>
      {hasSource && targets.isSuccess && !targets.isFetching && (
        <>
          <label htmlFor={`${id}-target`}>{t(($) => $['casdoorLocal.target'])}</label>
          <select
            id={`${id}-target`}
            className="block w-full rounded-lg border border-divider-regular p-2 focus-visible:outline-2 focus-visible:outline-components-button-primary-bg"
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
          >
            <option value="">{t(($) => $['casdoorLocal.target'])}</option>
            {targets.data.items.map((item) => (
              <option
                key={`${item.identity_id}/${item.workspace_id}`}
                value={`${item.identity_id}/${item.workspace_id}`}
              >
                {item.account_name} — {item.workspace_name}
              </option>
            ))}
          </select>
          {!targets.data.items.length && <p>{t(($) => $['casdoorIdentity.empty'])}</p>}
          {targets.data.has_more &&
            targets.data.next_identity_id &&
            targets.data.next_workspace_id && (
              <Button
                type="button"
                onClick={() => {
                  const data = targets.data
                  if (!data.next_identity_id || !data.next_workspace_id) return
                  setSelected('')
                  setCursor({
                    after_identity_id: data.next_identity_id,
                    after_workspace_id: data.next_workspace_id,
                  })
                }}
              >
                {t(($) => $['systemManage.casdoor.nextPage'])}
              </Button>
            )}
        </>
      )}
      {target && (
        <MembershipReview
          key={`${anchor}/${selected}`}
          identityId={target.identity_id}
          workspaceId={target.workspace_id}
        />
      )}
    </section>
  )
}
