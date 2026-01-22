import { RiContractLine, RiDoorLockLine, RiErrorWarningFill } from '@remixicon/react'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import * as React from 'react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Toast from '@/app/components/base/toast'
import { IS_CE_EDITION, CSRF_COOKIE_NAME } from '@/config'
import { useGlobalPublicStore } from '@/context/global-public-context'
import { invitationCheck } from '@/service/common'
import { useIsLogin } from '@/service/use-common'
import { LicenseStatus } from '@/types/feature'
import { cn } from '@/utils/classnames'
import Loading from '../components/base/loading'
import MailAndCodeAuth from './components/mail-and-code-auth'
import MailAndPasswordAuth from './components/mail-and-password-auth'
import SocialAuth from './components/social-auth'
import SSOAuth from './components/sso-auth'
import Split from './split'
import { resolvePostLoginRedirect } from './utils/post-login-redirect'
// Extend: start support ding_talk login
import DingTalkAuth from '@/app/signin/components/dingtalk-auth'
import OAuth2 from '@/app/signin/components/oauth2' // extend: add oauth2
// Extend: end

// Extend: start 声明一个变量来存储钉钉SDK
// 客户端环境中初始化钉钉SDK
let dd: any = null
if (typeof window !== 'undefined') {
  try {
    dd = require('dingtalk-jsapi')
  }
  catch (e) {
    console.error('Failed to load dingtalk-jsapi:', e)
  }
}
// Extend: end

const NormalForm = () => {
  const { t } = useTranslation()
  const router = useRouter()
  const searchParams = useSearchParams()
  const { isLoading: isCheckLoading, data: loginData } = useIsLogin()
  const isLoggedIn = loginData?.logged_in
  const message = decodeURIComponent(searchParams.get('message') || '')
  const invite_token = decodeURIComponent(searchParams.get('invite_token') || '')
  const [isInitCheckLoading, setInitCheckLoading] = useState(true)
  const [isRedirecting, setIsRedirecting] = useState(false)
  const isLoading = isCheckLoading || isInitCheckLoading || isRedirecting
  const { systemFeatures } = useGlobalPublicStore()
  const [authType, updateAuthType] = useState<'code' | 'password'>('password')
  const [showORLine, setShowORLine] = useState(false)
  const [allMethodsAreDisabled, setAllMethodsAreDisabled] = useState(false)
  const [workspaceName, setWorkSpaceName] = useState('')

  const isInviteLink = Boolean(invite_token && invite_token !== 'null')

  // Extend: start Ding Talk Auto Login Logic
  const dingTalkLogin = async (allFeatures: typeof systemFeatures) => {
    // 确保只在客户端环境执行
    if (typeof window === 'undefined' || !dd)
      return

    const tokenKey = CSRF_COOKIE_NAME()
    let consoleToken: string | null | undefined = decodeURIComponent(searchParams.get('console_token') || '')
    const consoleTokenFromLocalStorage = localStorage?.getItem(tokenKey)
    const jumpsNumber = Number(localStorage?.getItem('jumps_number'))
    if (consoleToken || consoleTokenFromLocalStorage) {
      if (!consoleToken)
        consoleToken = consoleTokenFromLocalStorage
      if (consoleToken) {
        if (jumpsNumber) {
          // token无效
          localStorage.removeItem(tokenKey)
          window.location.href = '/explore/apps-center-extend'
          return
        }
        localStorage.setItem(tokenKey, consoleToken)
        localStorage?.setItem('jumps_number', (jumpsNumber + 1).toString())
        window.location.href = `/explore/apps-center-extend?console_token=${consoleToken}`
        return
      }
      else {
        window.location.href = '/explore/apps-center-extend'
        return
      }
    }
    const userAgent = navigator.userAgent.toLowerCase()
    const host = process.env.NEXT_PUBLIC_API_PREFIX
    const corpId = allFeatures.ding_talk_corp_id
    if (userAgent.includes('dingtalk') && corpId && host) {
      // Extend Start DingTalk login compatible
      localStorage?.removeItem('redirect_url')
      // Extend Stop DingTalk login compatible

      try {
        await dd.getAuthCode({
          corpId,
          // 获取临时授权ID
          success: (res: { code: any }) => {
            // 在这里可以将免登授权码发送给后台服务器进行验证和获取用户信息等操作
            window.location.href = `${host}/ding-talk/login?code=${res.code}`
          },
          fail() {
            if (dd.runtime && dd.runtime.permission) {
              dd.runtime.permission.requestAuthCode({
                corpId,
                // 在这里我们移除了agentId参数，因为类型检查显示它不是有效的参数
                onSuccess(result: { code: any }) {
                  // 在这里可以将免登授权码发送给后台服务器进行验证和获取用户信息等操作
                  window.location.href = `${host}/ding-talk/login?code=${result.code}`
                },
              })
            }
          },
        })
      }
      catch (error) {
        console.error('DingTalk auth error:', error)
      }
    }
  }
  // Extend: end Ding Talk Auto Login Logic

  const init = useCallback(async () => {
    try {
      if (isLoggedIn) {
        setIsRedirecting(true)
        const redirectUrl = resolvePostLoginRedirect(searchParams)
        router.replace(redirectUrl || '/apps')
        return
      }

      if (message) {
        Toast.notify({
          type: 'error',
          message,
        })
      }
      setAllMethodsAreDisabled(!systemFeatures.enable_social_oauth_login && !systemFeatures.enable_email_code_login && !systemFeatures.enable_email_password_login && !systemFeatures.sso_enforced_for_signin && !systemFeatures.ding_talk && !systemFeatures.is_custom_auth2)
      setShowORLine((systemFeatures.enable_social_oauth_login || systemFeatures.sso_enforced_for_signin || !!systemFeatures.ding_talk || !!systemFeatures.is_custom_auth2) && (systemFeatures.enable_email_code_login || systemFeatures.enable_email_password_login))
      updateAuthType(systemFeatures.enable_email_password_login ? 'password' : 'code')

      // Extend: start 只在客户端执行钉钉登录
      if (typeof window !== 'undefined')
        await dingTalkLogin(systemFeatures)
      // Extend: end

      if (isInviteLink) {
        const checkRes = await invitationCheck({
          url: '/activate/check',
          params: {
            token: invite_token,
          },
        })
        setWorkSpaceName(checkRes?.data?.workspace_name || '')
      }
    }
    catch (error) {
      console.error(error)
      setAllMethodsAreDisabled(true)
    }
    finally { setInitCheckLoading(false) }
  }, [isLoggedIn, message, router, invite_token, isInviteLink, systemFeatures])
  useEffect(() => {
    init()
  }, [init])
  if (isLoading) {
    return (
      <div className={
        cn(
          'flex w-full grow flex-col items-center justify-center',
          'px-6',
          'md:px-[108px]',
        )
      }
      >
        <Loading type="area" />
      </div>
    )
  }
  if (systemFeatures.license?.status === LicenseStatus.LOST) {
    return (
      <div className="mx-auto mt-8 w-full">
        <div className="relative">
          <div className="rounded-lg bg-gradient-to-r from-workflow-workflow-progress-bg-1 to-workflow-workflow-progress-bg-2 p-4">
            <div className="shadows-shadow-lg relative mb-2 flex h-10 w-10 items-center justify-center rounded-xl bg-components-card-bg shadow">
              <RiContractLine className="h-5 w-5" />
              <RiErrorWarningFill className="absolute -right-1 -top-1 h-4 w-4 text-text-warning-secondary" />
            </div>
            <p className="system-sm-medium text-text-primary">{t('licenseLost', { ns: 'login' })}</p>
            <p className="system-xs-regular mt-1 text-text-tertiary">{t('licenseLostTip', { ns: 'login' })}</p>
          </div>
        </div>
      </div>
    )
  }
  if (systemFeatures.license?.status === LicenseStatus.EXPIRED) {
    return (
      <div className="mx-auto mt-8 w-full">
        <div className="relative">
          <div className="rounded-lg bg-gradient-to-r from-workflow-workflow-progress-bg-1 to-workflow-workflow-progress-bg-2 p-4">
            <div className="shadows-shadow-lg relative mb-2 flex h-10 w-10 items-center justify-center rounded-xl bg-components-card-bg shadow">
              <RiContractLine className="h-5 w-5" />
              <RiErrorWarningFill className="absolute -right-1 -top-1 h-4 w-4 text-text-warning-secondary" />
            </div>
            <p className="system-sm-medium text-text-primary">{t('licenseExpired', { ns: 'login' })}</p>
            <p className="system-xs-regular mt-1 text-text-tertiary">{t('licenseExpiredTip', { ns: 'login' })}</p>
          </div>
        </div>
      </div>
    )
  }
  if (systemFeatures.license?.status === LicenseStatus.INACTIVE) {
    return (
      <div className="mx-auto mt-8 w-full">
        <div className="relative">
          <div className="rounded-lg bg-gradient-to-r from-workflow-workflow-progress-bg-1 to-workflow-workflow-progress-bg-2 p-4">
            <div className="shadows-shadow-lg relative mb-2 flex h-10 w-10 items-center justify-center rounded-xl bg-components-card-bg shadow">
              <RiContractLine className="h-5 w-5" />
              <RiErrorWarningFill className="absolute -right-1 -top-1 h-4 w-4 text-text-warning-secondary" />
            </div>
            <p className="system-sm-medium text-text-primary">{t('licenseInactive', { ns: 'login' })}</p>
            <p className="system-xs-regular mt-1 text-text-tertiary">{t('licenseInactiveTip', { ns: 'login' })}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="mx-auto mt-8 w-full">
        {isInviteLink
          ? (
              <div className="mx-auto w-full">
                <h2 className="title-4xl-semi-bold text-text-primary">
                  {t('join', { ns: 'login' })}
                  {workspaceName}
                </h2>
                {!systemFeatures.branding.enabled && (
                  <p className="body-md-regular mt-2 text-text-tertiary">
                    {t('joinTipStart', { ns: 'login' })}
                    {workspaceName}
                    {t('joinTipEnd', { ns: 'login' })}
                  </p>
                )}
              </div>
            )
          : (
              <div className="mx-auto w-full">
                <h2 className="title-4xl-semi-bold text-text-primary">{systemFeatures.branding.enabled ? t('pageTitleForE', { ns: 'login' }) : t('pageTitle', { ns: 'login' })}</h2>
                <p className="body-md-regular mt-2 text-text-tertiary">{t('welcome', { ns: 'login' })}</p>
              </div>
            )}
        <div className="relative">
          <div className="mt-6 flex flex-col gap-3">
            {systemFeatures.enable_social_oauth_login && <SocialAuth />}
            {systemFeatures.sso_enforced_for_signin && (
              <div className="w-full">
                <SSOAuth protocol={systemFeatures.sso_enforced_for_signin_protocol} />
              </div>
            )}
            {/* Extend: start ding_talk login */}
            {systemFeatures.ding_talk && (<DingTalkAuth clientId={systemFeatures.ding_talk_client_id}></DingTalkAuth>)}
            {systemFeatures.is_custom_auth2 && (<OAuth2 title={systemFeatures.is_custom_auth2_button}></OAuth2>)}
            {/* Extend: end oauth2 login */}
          </div>

          {showORLine && (
            <div className="relative mt-6">
              <div className="flex items-center">
                <div className="h-px flex-1 bg-gradient-to-r from-background-gradient-mask-transparent to-divider-regular"></div>
                <span className="system-xs-medium-uppercase px-3 text-text-tertiary">{t('or', { ns: 'login' })}</span>
                <div className="h-px flex-1 bg-gradient-to-l from-background-gradient-mask-transparent to-divider-regular"></div>
              </div>
            </div>
          )}
          {
            (systemFeatures.enable_email_code_login || systemFeatures.enable_email_password_login) && (
              <>
                {systemFeatures.enable_email_code_login && authType === 'code' && (
                  <>
                    <MailAndCodeAuth isInvite={isInviteLink} />
                    {systemFeatures.enable_email_password_login && (
                      <div className="cursor-pointer py-1 text-center" onClick={() => { updateAuthType('password') }}>
                        <span className="system-xs-medium text-components-button-secondary-accent-text">{t('usePassword', { ns: 'login' })}</span>
                      </div>
                    )}
                  </>
                )}
                {systemFeatures.enable_email_password_login && authType === 'password' && (
                  <>
                    <MailAndPasswordAuth isInvite={isInviteLink} isEmailSetup={systemFeatures.is_email_setup} allowRegistration={systemFeatures.is_allow_register} />
                    {systemFeatures.enable_email_code_login && (
                      <div className="cursor-pointer py-1 text-center" onClick={() => { updateAuthType('code') }}>
                        <span className="system-xs-medium text-components-button-secondary-accent-text">{t('useVerificationCode', { ns: 'login' })}</span>
                      </div>
                    )}
                  </>
                )}
                <Split className="mb-5 mt-4" />
              </>
            )
          }

          {systemFeatures.is_allow_register && authType === 'password' && (
            <div className="mb-3 text-[13px] font-medium leading-4 text-text-secondary">
              <span>{t('signup.noAccount', { ns: 'login' })}</span>
              <Link
                className="text-text-accent"
                href="/signup"
              >
                {t('signup.signUp', { ns: 'login' })}
              </Link>
            </div>
          )}
          {allMethodsAreDisabled && (
            <>
              <div className="rounded-lg bg-gradient-to-r from-workflow-workflow-progress-bg-1 to-workflow-workflow-progress-bg-2 p-4">
                <div className="shadows-shadow-lg mb-2 flex h-10 w-10 items-center justify-center rounded-xl bg-components-card-bg shadow">
                  <RiDoorLockLine className="h-5 w-5" />
                </div>
                <p className="system-sm-medium text-text-primary">{t('noLoginMethod', { ns: 'login' })}</p>
                <p className="system-xs-regular mt-1 text-text-tertiary">{t('noLoginMethodTip', { ns: 'login' })}</p>
              </div>
              <div className="relative my-2 py-2">
                <div className="absolute inset-0 flex items-center" aria-hidden="true">
                  <div className="h-px w-full bg-gradient-to-r from-background-gradient-mask-transparent via-divider-regular to-background-gradient-mask-transparent"></div>
                </div>
              </div>
            </>
          )}
          {!systemFeatures.branding.enabled && (
            <>
              <div className="system-xs-regular mt-2 block w-full text-text-tertiary">
                {t('tosDesc', { ns: 'login' })}
              &nbsp;
                <Link
                  className="system-xs-medium text-text-secondary hover:underline"
                  target="_blank"
                  rel="noopener noreferrer"
                  href="https://dify.ai/terms"
                >
                  {t('tos', { ns: 'login' })}
                </Link>
              &nbsp;&&nbsp;
                <Link
                  className="system-xs-medium text-text-secondary hover:underline"
                  target="_blank"
                  rel="noopener noreferrer"
                  href="https://dify.ai/privacy"
                >
                  {t('pp', { ns: 'login' })}
                </Link>
              </div>
              {IS_CE_EDITION && (
                <div className="w-hull system-xs-regular mt-2 block text-text-tertiary">
                  {t('goToInit', { ns: 'login' })}
              &nbsp;
                  <Link
                    className="system-xs-medium text-text-secondary hover:underline"
                    href="/install"
                  >
                    {t('setAdminAccount', { ns: 'login' })}
                  </Link>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}

export default NormalForm
