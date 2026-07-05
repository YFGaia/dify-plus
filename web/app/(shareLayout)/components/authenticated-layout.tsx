'use client'

import * as React from 'react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import AppUnavailable from '@/app/components/base/app-unavailable'
import Loading from '@/app/components/base/loading'
import { useWebAppStore } from '@/context/web-app-context'
import { usePathname, useRouter, useSearchParams } from '@/next/navigation'
import { useGetUserCanAccessApp } from '@/service/access-control/use-app-access-control'
import { useGetWebAppInfo, useGetWebAppMeta, useGetWebAppParams } from '@/service/use-share'
import { checkConsoleLoginStatus, webAppLogout } from '@/service/webapp-auth'

const AuthenticatedLayout = ({ children }: { children: React.ReactNode }) => {
  const { t } = useTranslation()
  const shareCode = useWebAppStore(s => s.shareCode)
  const updateAppInfo = useWebAppStore(s => s.updateAppInfo)
  const updateAppParams = useWebAppStore(s => s.updateAppParams)
  const updateWebAppMeta = useWebAppStore(s => s.updateWebAppMeta)
  const updateUserCanAccessApp = useWebAppStore(s => s.updateUserCanAccessApp)
  const { isLoading: isLoadingAppParams, data: appParams, error: appParamsError } = useGetWebAppParams()
  const { isLoading: isLoadingAppInfo, data: appInfo, error: appInfoError } = useGetWebAppInfo()
  const { isLoading: isLoadingAppMeta, data: appMeta, error: appMetaError } = useGetWebAppMeta()
  const { data: userCanAccessApp, error: useCanAccessAppError } = useGetUserCanAccessApp({ appId: appInfo?.app_id, isInstalledApp: false })
  const [isCheckingAuth, setIsCheckingAuth] = useState(true)

  useEffect(() => {
    if (appInfo)
      updateAppInfo(appInfo)
    if (appParams)
      updateAppParams(appParams)
    if (appMeta)
      updateWebAppMeta(appMeta)
    updateUserCanAccessApp(Boolean(userCanAccessApp && userCanAccessApp?.result))
  }, [appInfo, appMeta, appParams, updateAppInfo, updateAppParams, updateUserCanAccessApp, updateWebAppMeta, userCanAccessApp])

  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()

  // 检查 Console 用户登录状态
  useEffect(() => {
    const checkConsoleAuth = async () => {
      setIsCheckingAuth(true)
      const isConsoleLoggedIn = await checkConsoleLoginStatus()
      if (!isConsoleLoggedIn) {
        // 未登录，保存当前 URL 到 localStorage，然后跳转到 Console 登录页面
        localStorage.setItem('redirect_url', pathname)
        router.replace('/signin')
      }
      setIsCheckingAuth(false)
    }

    checkConsoleAuth()
  }, [pathname, router])
  const getSigninUrl = useCallback(() => {
    const params = new URLSearchParams(searchParams)
    params.delete('message')
    const query = params.toString()
    const fullPath = query ? `${pathname}?${query}` : pathname
    params.set('redirect_url', fullPath)
    return `/webapp-signin?${params.toString()}`
  }, [searchParams, pathname])

  const backToHome = useCallback(async () => {
    await webAppLogout(shareCode!)
    const url = getSigninUrl()
    router.replace(url)
  }, [getSigninUrl, router, shareCode])

  if (appInfoError) {
    return (
      <div className="flex h-full items-center justify-center">
        <AppUnavailable unknownReason={appInfoError.message} />
      </div>
    )
  }
  if (appParamsError) {
    return (
      <div className="flex h-full items-center justify-center">
        <AppUnavailable unknownReason={appParamsError.message} />
      </div>
    )
  }
  if (appMetaError) {
    return (
      <div className="flex h-full items-center justify-center">
        <AppUnavailable unknownReason={appMetaError.message} />
      </div>
    )
  }
  if (useCanAccessAppError) {
    return (
      <div className="flex h-full items-center justify-center">
        <AppUnavailable unknownReason={useCanAccessAppError.message} />
      </div>
    )
  }
  if (userCanAccessApp && !userCanAccessApp.result) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-y-2">
        <AppUnavailable className="size-auto" code={403} unknownReason="no permission." />
        <span className="cursor-pointer system-sm-regular text-text-tertiary" onClick={backToHome}>{t('userProfile.logout', { ns: 'common' })}</span>
      </div>
    )
  }
  // extend: isCheckingAuth — WebApp 复用 Console 登录态的鉴权检查
  if (isCheckingAuth || isLoadingAppInfo || isLoadingAppParams || isLoadingAppMeta || !appInfo || !appParams || !appMeta) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loading />
      </div>
    )
  }
  return <>{children}</>
}

export default React.memo(AuthenticatedLayout)
