import { ACCESS_TOKEN_LOCAL_STORAGE_NAME, PASSPORT_LOCAL_STORAGE_NAME } from '@/config'
import { getPublic, postPublic } from './base'

export function setWebAppAccessToken(token: string) {
  localStorage.setItem(ACCESS_TOKEN_LOCAL_STORAGE_NAME, token)
}

export function setWebAppPassport(shareCode: string, token: string) {
  localStorage.setItem(PASSPORT_LOCAL_STORAGE_NAME(shareCode), token)
}

export function getWebAppAccessToken() {
  return localStorage.getItem(ACCESS_TOKEN_LOCAL_STORAGE_NAME) || ''
}

export function getWebAppPassport(shareCode: string) {
  return localStorage.getItem(PASSPORT_LOCAL_STORAGE_NAME(shareCode)) || ''
}

function clearWebAppAccessToken() {
  localStorage.removeItem(ACCESS_TOKEN_LOCAL_STORAGE_NAME)
}

function clearWebAppPassport(shareCode: string) {
  localStorage.removeItem(PASSPORT_LOCAL_STORAGE_NAME(shareCode))
}

type isWebAppLogin = {
  logged_in: boolean
  app_logged_in: boolean
  console_logged_in?: boolean
  // extend: 该 WebApp 的访问认证开关（false = 允许匿名访问）
  webapp_auth_enabled_extend?: boolean
}

export async function webAppLoginStatus(shareCode: string, userId?: string) {
  // always need to check login to prevent passport from being outdated
  // check remotely, the access token could be in cookie (enterprise SSO redirected with https)
  const params = new URLSearchParams({ app_code: shareCode })
  if (userId) params.append('user_id', userId)
  const { logged_in, app_logged_in } = await getPublic<isWebAppLogin>(
    `/login/status?${params.toString()}`,
  )
  return {
    userLoggedIn: logged_in,
    appLoggedIn: app_logged_in,
  }
}

export async function checkConsoleLoginStatus() {
  try {
    const { console_logged_in } = await getPublic<isWebAppLogin>('/login/status')
    return console_logged_in || false
  } catch (error) {
    console.error('Failed to check console login status:', error)
    return false
  }
}

// extend: 按 app_code 同时查询 Console 登录态与该 WebApp 的访问认证开关；
// 请求失败按「需认证且未登录」处理（fail-closed，与 checkConsoleLoginStatus 一致）
export type WebAppConsoleAuthStatus = {
  consoleLoggedIn: boolean
  webAppAuthEnabled: boolean
}

export async function checkWebAppConsoleAuthStatus(
  shareCode: string,
): Promise<WebAppConsoleAuthStatus> {
  try {
    const params = new URLSearchParams({ app_code: shareCode })
    const { console_logged_in, webapp_auth_enabled_extend } = await getPublic<isWebAppLogin>(
      `/login/status?${params.toString()}`,
    )
    return {
      consoleLoggedIn: console_logged_in || false,
      webAppAuthEnabled: webapp_auth_enabled_extend ?? true,
    }
  } catch (error) {
    // app_code 无效（404）等场景回退到无参检查，保持既有「已登录用户看到 App 不可用页」的行为
    console.error('Failed to check webapp console auth status:', error)
    const consoleLoggedIn = await checkConsoleLoginStatus()
    return { consoleLoggedIn, webAppAuthEnabled: true }
  }
}

export async function webAppLogout(shareCode: string) {
  clearWebAppAccessToken()
  clearWebAppPassport(shareCode)
  await postPublic('/logout')
}
