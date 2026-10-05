'use client'

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from '#i18n'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { casdoorSessionQueryOptions } from './client'

export function CasdoorSessionNotice() {
  const client = useQueryClient()
  const { t } = useTranslation('extend')
  // Subscribe to the existing profile cache without adding a second fetch owner.
  const { data: profileAccountId } = useQuery({
    ...userProfileQueryOptions(),
    enabled: false,
    select: (value) => value.profile.id,
  })
  const session = useQuery(casdoorSessionQueryOptions(client, profileAccountId))
  const refetch = session.refetch
  const [now, setNow] = useState(Date.now)
  const expires = session.data?.expires_at ? Date.parse(session.data.expires_at) : null
  useEffect(() => {
    if (expires === null || !Number.isFinite(expires) || expires <= now) return
    const timer = window.setTimeout(
      () => {
        setNow(Date.now())
        void refetch()
      },
      Math.min(Math.max(expires - Date.now(), 0), 2_147_483_647),
    )
    return () => window.clearTimeout(timer)
  }, [expires, now, refetch])
  if (!session.data || session.data.source !== 'casdoor' || expires === null || expires <= now)
    return null
  return (
    <div className="px-3 py-2 text-xs text-text-tertiary">
      <p>{t(($) => $['casdoorSession.source'])}</p>
      <p>
        {session.data.rp_logout_available
          ? t(($) => $['casdoorSession.enterpriseAttempt'])
          : t(($) => $['casdoorSession.localOnly'])}
      </p>
    </div>
  )
}
