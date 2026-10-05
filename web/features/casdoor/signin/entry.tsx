import type { CasdoorDisplayResponse } from '@dify/contracts/api/console/auth/types.gen'
import type { UseQueryResult } from '@tanstack/react-query'
import { Button, buttonVariants } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { useTranslation } from 'react-i18next'
import { API_PREFIX } from '@/config'
import { useLocale } from '@/context/i18n'
import { useSearchParams } from '@/next/navigation'
import { resolveLoginRedirectTarget } from '@/utils/login-redirect'
import { getBrowserTimezone } from '@/utils/timezone'

function isStrictInternalPath(value: string) {
  return (
    value.startsWith('/') &&
    value.length <= 2048 &&
    value === value.trim() &&
    !/[?#%\\\u0000-\u001F\u007F]/.test(value) &&
    !value.includes('//') &&
    !value.split('/').some((segment) => segment === '.' || segment === '..')
  )
}

function getReturnPath(candidates: string[]) {
  const candidate = candidates.length === 1 ? candidates[0] : undefined
  if (!candidate || !isStrictInternalPath(candidate)) return '/apps'
  const target = resolveLoginRedirectTarget(candidate)
  return target?.kind === 'internal' && isStrictInternalPath(target.href) ? target.href : '/apps'
}

function validLocale(value: string) {
  return value.length <= 64 && /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/i.test(value)
}

function validTimezone(value: string | undefined): value is string {
  if (!value || value.length > 64 || !/^[\w+/-]+$/.test(value)) return false
  try {
    new Intl.DateTimeFormat('en', { timeZone: value }).resolvedOptions()
    return true
  } catch {
    return false
  }
}

// oxlint-disable-next-line react/only-export-components -- Share the existing bounded query parser with the sign-in owner.
export function readValidatedCasdoorInvitation(params: Pick<URLSearchParams, 'getAll'>) {
  const invitations = params.getAll('invite_token')
  if (invitations.length > 1) return null
  const invitation = invitations[0]
  if (!invitation || invitation === 'null') return undefined
  if (
    invitation.length > 512 ||
    invitation !== invitation.trim() ||
    /[\u0000-\u001F\u007F]/.test(invitation)
  )
    return null
  return invitation
}

export default function CasdoorSigninEntry({
  query,
}: {
  query: UseQueryResult<CasdoorDisplayResponse>
}) {
  const { t } = useTranslation()
  const searchParams = useSearchParams()
  const locale = useLocale()
  if (query.isPending || query.isFetching) {
    return (
      <p role="status" className="system-sm-regular text-text-secondary">
        {t(($) => $.loading, { ns: 'common' })}
      </p>
    )
  }
  if (query.isError) {
    return (
      <div className="flex flex-col gap-2">
        <p role="alert" className="system-sm-regular text-text-secondary">
          {t(($) => $['api.actionFailed'], { ns: 'common' })}
        </p>
        <Button
          onClick={() => {
            void query.refetch()
          }}
        >
          {t(($) => $['operation.retry'], { ns: 'common' })}
        </Button>
      </div>
    )
  }
  if (!query.isSuccess || !query.data.enabled) return null

  const params = new URLSearchParams({
    return_path: getReturnPath(searchParams.getAll('redirect_url')),
  })
  const invitation = readValidatedCasdoorInvitation(searchParams)
  if (invitation === null) return null
  if (invitation) params.set('invite_token', invitation)
  if (validLocale(locale)) params.set('locale', locale)
  const timezone = getBrowserTimezone()
  if (validTimezone(timezone)) params.set('timezone', timezone)
  const href = `${API_PREFIX.replace(/\/+$/, '')}/auth/casdoor/login?${params}`
  return (
    <a className={cn(buttonVariants(), 'w-full')} href={href}>
      {query.data.button_text}
    </a>
  )
}
