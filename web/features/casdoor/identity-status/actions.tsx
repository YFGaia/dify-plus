'use client'

import type extend from '@/i18n/en-US/extend.json'
import { Button } from '@langgenius/dify-ui/button'
import {
  hashKey,
  notifyManager,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useId, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { useSearchParams } from '@/next/navigation'
import { parseIdentityNavigation } from '../identity-navigation'
import {
  identityActionsQueryOptions,
  identityLinkMutationOptions,
  identitySourceGuard,
  identityUnlinkMutationOptions,
} from './action-client'

type CopyKey = Extract<keyof typeof extend, `casdoorIdentity.${string}`>
export function IdentityActions({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const id = useId()
  const [confirmed, setConfirmed] = useState(false)
  const [failed, setFailed] = useState(false)
  const [unlinked, setUnlinked] = useState(false)
  const returned = useSearchParams().get('casdoor_identity')
  const options = identityActionsQueryOptions(client)
  const [sourceAtMount] = useState(() => identitySourceGuard(client).anchor)
  const profileHash = hashKey(userProfileQueryOptions().queryKey)
  const hasSource = useSyncExternalStore(
    (notify) =>
      client.getQueryCache().subscribe(
        notifyManager.batchCalls((event) => {
          if (event.query.queryHash === profileHash) notify()
        }),
      ),
    () => typeof sourceAtMount === 'string' && identitySourceGuard(client).anchor === sourceAtMount,
    () => false,
  )
  const canRead = enabled && hasSource
  const actions = useQuery({ ...options, enabled: canRead && options.enabled })
  const link = useMutation(identityLinkMutationOptions(client, 'link'))
  const reauth = useMutation(identityLinkMutationOptions(client, 'reauthenticate'))
  const unlink = useMutation(identityUnlinkMutationOptions(client))
  const busy = link.isPending || reauth.isPending || unlink.isPending
  // Availability is advisory; each server POST validates the current source again.
  const current = canRead && actions.isSuccess && !actions.isFetching ? actions.data : undefined
  const copy = (key: CopyKey) => t(($) => $[key], { ns: 'extend' })
  const reason =
    current?.reason === 'managed_history_requires_release'
      ? 'casdoorIdentity.historyRequiresRelease'
      : current?.reason === 'other_login_unavailable'
        ? 'casdoorIdentity.recoveryUnavailable'
        : current?.reason === 'reauthentication_unavailable'
          ? 'casdoorIdentity.reauthUnavailable'
          : null
  const navigate = (kind: 'link' | 'reauthenticate') => {
    if (!current?.[kind] || busy) return
    setFailed(false)
    const mutation = kind === 'link' ? link : reauth
    mutation.mutate(
      { body: {} },
      {
        onSuccess: (data) => {
          try {
            window.location.assign(parseIdentityNavigation(data))
          } catch {
            setFailed(true)
          }
        },
        onError: () => {
          setFailed(true)
          void actions.refetch().catch(() => {})
        },
      },
    )
  }
  return (
    <div className="space-y-3">
      {(failed || returned === 'failed' || returned === 'cancelled' || actions.isError) && (
        <p role="alert">{copy('casdoorIdentity.actionIncomplete')}</p>
      )}
      {unlinked && <p role="status">{copy('casdoorIdentity.unlinked')}</p>}
      {reason && <p role="status">{copy(reason)}</p>}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          loading={actions.isFetching}
          disabled={!canRead || !options.enabled || busy}
          onClick={() => {
            setFailed(false)
            void actions.refetch().catch(() => {})
          }}
        >
          {t(($) => $['systemManage.casdoor.refresh'], { ns: 'extend' })}
        </Button>
        <Button
          type="button"
          loading={link.isPending}
          disabled={busy || !current?.link}
          onClick={() => navigate('link')}
        >
          {copy('casdoorIdentity.linkAction')}
        </Button>
        <Button
          type="button"
          loading={reauth.isPending}
          disabled={busy || !current?.reauthenticate || current.unlink}
          onClick={() => navigate('reauthenticate')}
        >
          {copy('casdoorIdentity.reauthenticateAction')}
        </Button>
      </div>
      {current?.unlink && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!confirmed || busy || !current.unlink) return
            setFailed(false)
            unlink.mutate(
              { body: {} },
              {
                onSuccess: () => {
                  setConfirmed(false)
                  setUnlinked(true)
                },
                onError: () => {
                  setFailed(true)
                  setConfirmed(false)
                },
              },
            )
          }}
        >
          <p id={`${id}-warning`}>{copy('casdoorIdentity.unlinkWarning')}</p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              aria-describedby={`${id}-warning`}
            />
            {copy('casdoorIdentity.confirmUnlink')}
          </label>
          <Button type="submit" loading={unlink.isPending} disabled={!confirmed || busy}>
            {copy('casdoorIdentity.unlinkAction')}
          </Button>
        </form>
      )}
    </div>
  )
}
