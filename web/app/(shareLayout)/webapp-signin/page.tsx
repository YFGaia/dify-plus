'use client'
import type { FC } from 'react'
import { useSuspenseQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import AppUnavailable from '@/app/components/base/app-unavailable'
import Loading from '@/app/components/base/loading'
import { useWebAppStore } from '@/context/web-app-context'
import { systemFeaturesQueryOptions } from '@/features/system-features/client'
import { AccessMode } from '@/models/access-control'
import { useRouter, useSearchParams } from '@/next/navigation'
// extend: WebApp 登录复用 Console 登录态
import { checkConsoleLoginStatus, webAppLogout } from '@/service/webapp-auth'
import ExternalMemberSsoAuth from './components/external-member-sso-auth'
import NormalForm from './normalForm'

const WebSSOForm: FC = () => {
  const { t } = useTranslation()
  const { data: systemFeatures } = useSuspenseQuery(systemFeaturesQueryOptions())
  const webAppAccessMode = useWebAppStore(s => s.webAppAccessMode)
  const searchParams = useSearchParams()
  const router = useRouter()
  const [isCheckingAuth, setIsCheckingAuth] = useState(true)

  const redirectUrl = searchParams.get('redirect_url')

  // 检查 Console 用户登录状态
  useEffect(() => {
    const checkAuth = async () => {
      setIsCheckingAuth(true)
      const isConsoleLoggedIn = await checkConsoleLoginStatus()
      if (!isConsoleLoggedIn) {
        // 未登录，保存 redirect_url 到 localStorage，然后跳转到 Console 登录页面
        if (redirectUrl)
          localStorage.setItem('redirect_url', redirectUrl)
        router.replace('/signin')
      }
      setIsCheckingAuth(false)
    }

    checkAuth()
  }, [router, redirectUrl])

  const getSigninUrl = useCallback(() => {
    const params = new URLSearchParams()
    params.append('redirect_url', redirectUrl || '')
    return `/webapp-signin?${params.toString()}`
  }, [redirectUrl])

  const shareCode = useWebAppStore(s => s.shareCode)
  const backToHome = useCallback(async () => {
    await webAppLogout(shareCode!)
    const url = getSigninUrl()
    router.replace(url)
  }, [getSigninUrl, router, webAppLogout, shareCode])

  if (isCheckingAuth) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loading />
      </div>
    )
  }

  if (!redirectUrl) {
    return (
      <div className="flex h-full items-center justify-center">
        <AppUnavailable code={t('common.appUnavailable', { ns: 'share' })} unknownReason="redirect url is invalid." />
      </div>
    )
  }

  if (!systemFeatures.webapp_auth.enabled) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="system-xs-regular text-text-tertiary">{t('webapp.disabled', { ns: 'login' })}</p>
      </div>
    )
  }
  if (webAppAccessMode && (webAppAccessMode === AccessMode.ORGANIZATION || webAppAccessMode === AccessMode.SPECIFIC_GROUPS_MEMBERS)) {
    return (
      <div className="w-full max-w-[400px]">
        <NormalForm />
      </div>
    )
  }

  if (webAppAccessMode && webAppAccessMode === AccessMode.EXTERNAL_MEMBERS)
    return <ExternalMemberSsoAuth />

  return (
    <div className="flex h-full flex-col items-center justify-center gap-y-4">
      <AppUnavailable className="size-auto" isUnknownReason={true} />
      <span className="cursor-pointer system-sm-regular text-text-tertiary" onClick={backToHome}>{t('login.backToHome', { ns: 'share' })}</span>
    </div>
  )
}

export default React.memo(WebSSOForm)
