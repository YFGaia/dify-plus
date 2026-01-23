import type { ReadonlyURLSearchParams } from 'next/navigation'
import dayjs from 'dayjs'
import { OAUTH_AUTHORIZE_PENDING_KEY, REDIRECT_URL_KEY } from '@/app/account/oauth/authorize/constants'

function getItemWithExpiry(key: string): string | null {
  const itemStr = localStorage.getItem(key)
  if (!itemStr)
    return null

  try {
    const item = JSON.parse(itemStr)
    localStorage.removeItem(key)
    if (!item?.value)
      return null

    return dayjs().unix() > item.expiry ? null : item.value
  }
  catch {
    return null
  }
}

export const resolvePostLoginRedirect = (searchParams: ReadonlyURLSearchParams) => {
  // WebApp/Console: 优先走 localStorage 里的 redirect_url（登录成功后必须清理）
  const localRedirectUrl = localStorage.getItem('redirect_url')
  if (localRedirectUrl) {
    localStorage.removeItem('redirect_url')
    try {
      return decodeURIComponent(localRedirectUrl)
    }
    catch (e) {
      console.error('Failed to decode redirect URL from localStorage:', e)
      return localRedirectUrl
    }
  }

  const redirectUrl = searchParams.get(REDIRECT_URL_KEY)
  if (redirectUrl) {
    try {
      localStorage.removeItem(OAUTH_AUTHORIZE_PENDING_KEY)
      return decodeURIComponent(redirectUrl)
    }
    catch (e) {
      console.error('Failed to decode redirect URL:', e)
      return redirectUrl
    }
  }

  return getItemWithExpiry(OAUTH_AUTHORIZE_PENDING_KEY)
}
