'use client'

import type {
  CasdoorSelfCurrentMembershipResponse,
  CasdoorSelfIdentityResponse,
  CasdoorSelfMembershipResponse,
  CasdoorSelfNameResponse,
  GetAccountCasdoorIdentityData,
  GetAccountCasdoorIdentityResponse,
} from '@dify/contracts/api/console/account/types.gen'
import type extend from '@/i18n/en-US/extend.json'
import { Button } from '@langgenius/dify-ui/button'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { casdoorIdentityQueryOptions } from './client'

type CopyKey = Extract<keyof typeof extend, `casdoorIdentity.${string}`>
const bindingCopy = {
  linked: 'casdoorIdentity.linked',
  unlinked: 'casdoorIdentity.unlinked',
} satisfies Record<GetAccountCasdoorIdentityResponse['binding'], CopyKey>
const activityCopy = {
  active: 'casdoorIdentity.active',
  inactive: 'casdoorIdentity.inactive',
  unknown: 'casdoorIdentity.unknown',
} satisfies Record<CasdoorSelfIdentityResponse['activity'], CopyKey>
const lifecycleCopy = {
  active: 'casdoorIdentity.active',
  archived: 'casdoorIdentity.archived',
  fencing: 'casdoorIdentity.fencing',
  unknown: 'casdoorIdentity.unknown',
} satisfies Record<CasdoorSelfIdentityResponse['lifecycle'], CopyKey>
const consistencyCopy = {
  consistent: 'casdoorIdentity.consistent',
  historical: 'casdoorIdentity.historical',
  stale: 'casdoorIdentity.stale',
  unknown: 'casdoorIdentity.unknown',
} satisfies Record<
  CasdoorSelfIdentityResponse['profile_consistency'] | CasdoorSelfMembershipResponse['consistency'],
  CopyKey
>
const nameCopy = {
  applied: 'casdoorIdentity.applied',
  disabled: 'casdoorIdentity.disabled',
  local_override: 'casdoorIdentity.override',
  skipped: 'casdoorIdentity.skipped',
  unchanged: 'casdoorIdentity.unchanged',
} satisfies Record<NonNullable<CasdoorSelfNameResponse['last_status']>, CopyKey>
const ownershipCopy = {
  local_override: 'casdoorIdentity.override',
  managed: 'casdoorIdentity.managed',
  released: 'casdoorIdentity.released',
} satisfies Record<NonNullable<CasdoorSelfMembershipResponse['recorded_ownership']>, CopyKey>
const historyCopy = {
  absent_unknown: 'casdoorIdentity.unknown',
  controlled_withdrawal: 'casdoorIdentity.withdrawal',
  historical: 'casdoorIdentity.historical',
  local_override: 'casdoorIdentity.override',
  recorded_managed: 'casdoorIdentity.managed',
  tombstone: 'casdoorIdentity.tombstone',
  unknown: 'casdoorIdentity.unknown',
  unmanaged: 'casdoorIdentity.unmanaged',
} satisfies Record<CasdoorSelfMembershipResponse['state'], CopyKey>
const currentCopy = {
  history_present: 'casdoorIdentity.historical',
  unknown: 'casdoorIdentity.unknown',
  unmanaged: 'casdoorIdentity.unmanaged',
} satisfies Record<CasdoorSelfCurrentMembershipResponse['state'], CopyKey>
const presenceCopy = {
  absent: 'casdoorIdentity.absent',
  present: 'casdoorIdentity.present',
  unknown: 'casdoorIdentity.unknown',
} satisfies Record<CasdoorSelfCurrentMembershipResponse['join_presence'], CopyKey>
const roleCopy = {
  admin: 'systemManage.casdoor.admin',
  dataset_operator: 'casdoorIdentity.roleDatasetOperator',
  editor: 'systemManage.casdoor.editor',
  normal: 'systemManage.casdoor.normal',
  owner: 'casdoorIdentity.roleOwner',
} satisfies Record<
  NonNullable<CasdoorSelfCurrentMembershipResponse['local_role']>,
  keyof typeof extend
>

export default function EnterpriseIdentityPanel() {
  const { t } = useTranslation()
  const id = useId()
  const client = useQueryClient()
  const profileId = client.getQueryData(userProfileQueryOptions().queryKey)?.profile?.id
  const [profileIdAtMount] = useState(profileId)
  // Query observers do not notify on cache removal; subscribe to the existing
  // profile cache so logout/removal immediately hides any previously read identity.
  const hasProfileAnchor = useSyncExternalStore(
    (onChange) => client.getQueryCache().subscribe(onChange),
    () => {
      const profile = client.getQueryState(userProfileQueryOptions().queryKey)
      const anchor = profile?.data?.profile?.id
      return profile?.status !== 'error' && typeof anchor === 'string' && anchor.trim().length > 0
    },
    () => false,
  )
  const [identityAfter, setIdentityAfter] = useState<string>()
  const [membershipAfter, setMembershipAfter] = useState<string>()
  const [currentMembershipAfter, setCurrentMembershipAfter] = useState<string>()
  const query: GetAccountCasdoorIdentityData['query'] = {
    limit: 20,
    ...(identityAfter === undefined ? {} : { identity_after: identityAfter }),
    ...(membershipAfter === undefined ? {} : { membership_after: membershipAfter }),
    ...(currentMembershipAfter === undefined
      ? {}
      : { current_membership_after: currentMembershipAfter }),
  }
  const options = casdoorIdentityQueryOptions(client, query)
  // The parent owns account-key remounts; the old instance must not fetch for B first.
  const canReadProfile =
    hasProfileAnchor && (profileIdAtMount === undefined || profileIdAtMount === profileId)
  // Availability recovery must re-enable a fresh self read even for the same profile ID.
  const result = useQuery({ ...options, enabled: options.enabled && canReadProfile })
  const copy = (key: CopyKey) => t(($) => $[key], { ns: 'extend' })
  const unknown = copy('casdoorIdentity.unknown')
  const boolean = (value: boolean | null) =>
    value === null ? unknown : copy(value ? 'casdoorIdentity.yes' : 'casdoorIdentity.no')
  const role = (value: CasdoorSelfCurrentMembershipResponse['local_role']) =>
    value === null ? unknown : t(($) => $[roleCopy[value]], { ns: 'extend' })
  const unavailable = !options.enabled || !canReadProfile || result.isError
  const data = unavailable || !result.isSuccess || result.isFetching ? undefined : result.data
  const fields = (rows: [string, string][]) => (
    <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
      {rows.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-text-tertiary">{label}</dt>
          <dd className="wrap-break-word text-text-secondary">{value}</dd>
        </div>
      ))}
    </dl>
  )
  const pages = [
    {
      key: 'identities',
      cursor: identityAfter,
      setCursor: setIdentityAfter,
      hasMore: data?.identity_has_more,
      next: data?.identity_next,
    },
    {
      key: 'history',
      cursor: membershipAfter,
      setCursor: setMembershipAfter,
      hasMore: data?.membership_has_more,
      next: data?.membership_next,
    },
    {
      key: 'current',
      cursor: currentMembershipAfter,
      setCursor: setCurrentMembershipAfter,
      hasMore: data?.current_membership_has_more,
      next: data?.current_membership_next,
    },
  ] as const

  return (
    <section
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-guidance`}
      className="mb-8 space-y-4"
    >
      <h2 id={`${id}-title`} className="system-sm-semibold text-text-secondary">
        {copy('casdoorIdentity.title')}
      </h2>
      <p id={`${id}-guidance`} className="body-xs-regular text-text-tertiary">
        {copy('casdoorIdentity.guidance')}
      </p>
      {unavailable ? (
        <div className="space-y-2">
          <p role="alert">{copy('casdoorIdentity.unavailable')}</p>
          <Button
            disabled={!options.enabled || !canReadProfile}
            onClick={() => {
              void result.refetch().catch(() => {})
            }}
          >
            {t(($) => $['operation.retry'], { ns: 'common' })}
          </Button>
        </div>
      ) : !data ? (
        <div role="status" className="rounded-lg bg-background-section p-4">
          {t(($) => $.loading, { ns: 'common' })}
        </div>
      ) : (
        <>
          <p role="status">{copy(bindingCopy[data.binding])}</p>
          {pages.map((page) => (
            <section key={page.key} aria-labelledby={`${id}-${page.key}`} className="space-y-3">
              <h3 id={`${id}-${page.key}`} className="system-sm-semibold text-text-secondary">
                {copy(`casdoorIdentity.${page.key}`)}
              </h3>
              {page.key === 'identities' &&
                (data.identities.length ? (
                  data.identities.map((identity, index) => (
                    <div
                      key={identity.id ?? index}
                      className="space-y-3 rounded-lg border border-divider-subtle p-3"
                    >
                      {fields([
                        [copy('casdoorIdentity.organization'), identity.organization ?? unknown],
                        [copy('casdoorIdentity.identifier'), identity.masked_identifier],
                        [copy('casdoorIdentity.activity'), copy(activityCopy[identity.activity])],
                        [
                          copy('casdoorIdentity.lifecycle'),
                          copy(lifecycleCopy[identity.lifecycle]),
                        ],
                        [
                          copy('casdoorIdentity.consistency'),
                          copy(consistencyCopy[identity.profile_consistency]),
                        ],
                        [copy('casdoorIdentity.avatar'), unknown],
                      ])}
                      <h4 className="system-xs-semibold text-text-secondary">
                        {t(($) => $['account.name'], { ns: 'common' })}
                      </h4>
                      {fields([
                        [
                          copy('casdoorIdentity.nameStatus'),
                          identity.name.last_status === null
                            ? unknown
                            : copy(nameCopy[identity.name.last_status]),
                        ],
                        [copy('casdoorIdentity.nameTime'), identity.name.last_sync_at ?? unknown],
                        [
                          copy('casdoorIdentity.nameDiff'),
                          boolean(identity.name.current_local_differs_from_last_applied),
                        ],
                      ])}
                      <h4 className="system-xs-semibold text-text-secondary">
                        {t(($) => $['account.email'], { ns: 'common' })}
                      </h4>
                      {fields([
                        [
                          copy('casdoorIdentity.emailDiff'),
                          boolean(identity.email.current_differs),
                        ],
                        [
                          copy('casdoorIdentity.emailLastDiff'),
                          boolean(identity.email.last_differs),
                        ],
                        [copy('casdoorIdentity.emailVerified'), boolean(identity.email.verified)],
                      ])}
                    </div>
                  ))
                ) : (
                  <p>{copy('casdoorIdentity.empty')}</p>
                ))}
              {page.key === 'history' &&
                (data.memberships.length ? (
                  data.memberships.map((membership, index) => (
                    <div
                      key={membership.id ?? index}
                      className="rounded-lg border border-divider-subtle p-3"
                    >
                      {fields([
                        [copy('casdoorIdentity.workspace'), membership.workspace_id ?? unknown],
                        [copy('casdoorIdentity.localRole'), role(membership.local_role)],
                        [
                          copy('casdoorIdentity.ownership'),
                          membership.recorded_ownership === null
                            ? unknown
                            : copy(ownershipCopy[membership.recorded_ownership]),
                        ],
                        [
                          copy('casdoorIdentity.membershipState'),
                          copy(historyCopy[membership.state]),
                        ],
                        [
                          copy('casdoorIdentity.consistency'),
                          copy(consistencyCopy[membership.consistency]),
                        ],
                        [
                          copy('casdoorIdentity.presence'),
                          copy(presenceCopy[membership.join_presence]),
                        ],
                        [copy('casdoorIdentity.remote'), unknown],
                      ])}
                    </div>
                  ))
                ) : (
                  <p>{copy('casdoorIdentity.empty')}</p>
                ))}
              {page.key === 'current' &&
                (data.current_memberships.length ? (
                  data.current_memberships.map((membership, index) => (
                    <div
                      key={membership.id ?? index}
                      className="rounded-lg border border-divider-subtle p-3"
                    >
                      {fields([
                        [copy('casdoorIdentity.workspace'), membership.workspace_id ?? unknown],
                        [copy('casdoorIdentity.localRole'), role(membership.local_role)],
                        [
                          copy('casdoorIdentity.membershipState'),
                          copy(currentCopy[membership.state]),
                        ],
                        [
                          copy('casdoorIdentity.presence'),
                          copy(presenceCopy[membership.join_presence]),
                        ],
                        [copy('casdoorIdentity.remote'), unknown],
                      ])}
                    </div>
                  ))
                ) : (
                  <p>{copy('casdoorIdentity.empty')}</p>
                ))}
              <div className="flex flex-wrap gap-2">
                <Button
                  disabled={page.cursor === undefined}
                  aria-describedby={`${id}-${page.key}`}
                  onClick={() => page.setCursor(undefined)}
                >
                  {copy('casdoorIdentity.firstPage')}
                </Button>
                <Button
                  disabled={page.hasMore !== true || !page.next}
                  aria-describedby={`${id}-${page.key}`}
                  onClick={() => {
                    if (page.hasMore === true && page.next) page.setCursor(page.next)
                  }}
                >
                  {t(($) => $['pagination.next'], { ns: 'common' })}
                </Button>
              </div>
            </section>
          ))}
        </>
      )}
    </section>
  )
}
