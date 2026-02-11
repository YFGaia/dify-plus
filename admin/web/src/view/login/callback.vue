<template>
  <div class="flex items-center justify-center min-h-screen">
    <span>登录中...</span>
  </div>
</template>

<script setup>
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { useUserStore } from '@/pinia/modules/user'
import { useRouterStore } from '@/pinia/modules/router'
import router from '@/router'
import { gaiaOAuth2Login, dingtalkLogin } from '@/api/user_extend'

defineOptions({
  name: 'LoginCallback',
})
const route = useRoute()
const userStore = useUserStore()

const redirectToThirdParty = (token, redirectUri, state) => {
  if (!redirectUri) return false
  sessionStorage.removeItem('gaia_login_redirect_uri')
  sessionStorage.removeItem('gaia_login_state')
  const sep = redirectUri.includes('?') ? '&' : '?'
  const url = redirectUri + sep + 'token=' + encodeURIComponent(token) + (state ? '&state=' + encodeURIComponent(state) : '')
  window.location.href = url
  window.location.href = "/"
  return true
}

const goToDashboard = async () => {
  const routerStore = useRouterStore()
  await routerStore.SetAsyncRouter()
  routerStore.asyncRouters.forEach(r => router.addRoute(r))
  const name = userStore.userInfo?.authority?.defaultRouter || 'gaiaDashboard'
  await router.replace({ name: name || 'gaiaDashboard' })
}

const failAndBackToLogin = (msg) => {
  ElMessage({ type: 'error', message: msg || '登录失败，3秒后跳转到登录页', showClose: true })
  setTimeout(() => { window.location.href = '/#/login' }, 3000)
}

// 钉钉/OAuth 回调时 code、authCode 可能在 hash 前的主 URL query 中（如 /admin/?code=xx&authCode=xx&state=dingtalk#/loginCallback?provider=dingtalk）
const getQueryParam = (name) => {
  const fromRoute = route.query[name]
  if (fromRoute) return fromRoute
  const search = window.location.search
  if (!search) return ''
  const params = new URLSearchParams(search)
  return params.get(name) || ''
}

const callback = async () => {
  const provider = getQueryParam('provider') || route.query.provider
  const code = getQueryParam('code') || getQueryParam('authCode') || route.query.code || route.query.authCode
  // Extend Start: 兼容 casdoor（部分 OAuth 如 Casdoor 可能通过 implicit/hybrid 直接回传 access_token，无 code）
  const accessTokenFromQuery = getQueryParam('access_token') || route.query.access_token || ''
  const hasCode = !!code
  const hasAccessToken = !!accessTokenFromQuery
  if (!hasCode && !hasAccessToken) {
    failAndBackToLogin('授权码或 access_token 缺失，3秒后跳转到登录页')
    return
  }
  // Extend Stop: 兼容 casdoor

  const redirectUri = sessionStorage.getItem('gaia_login_redirect_uri') || ''
  const state = sessionStorage.getItem('gaia_login_state') || getQueryParam('state') || ''

  try {
    if (provider === 'dingtalk') {
      if (!hasCode) {
        failAndBackToLogin('钉钉登录需要授权码')
        return
      }
      const res = await dingtalkLogin({ auth_code: code, redirect_uri: redirectUri, state })
      if (res?.code === 0 && res.data?.token) {
        userStore.setUserInfo(res.data.user)
        userStore.setToken(res.data.token)
        // 优先用接口返回的 redirect_uri/state（用户可能从应用直接跳到钉钉，未经过登录页，sessionStorage 为空）
        const finalRedirect = res.data.redirect_uri || redirectUri
        const finalState = res.data.state ?? state
        if (redirectToThirdParty(res.data.token, finalRedirect, finalState)) return
        await goToDashboard()
        return
      }
    } else if (provider === 'oauth2') {
      // Extend Start: 兼容 casdoor（支持仅带 access_token 的回调）
      const payload = hasCode
        ? { code, redirect_uri: redirectUri, state }
        : { access_token: accessTokenFromQuery, redirect_uri: redirectUri, state }
      const res = await gaiaOAuth2Login(payload)
      // Extend Stop: 兼容 casdoor
      if (res?.code === 0 && res.data?.token) {
        userStore.setUserInfo(res.data.user)
        userStore.setToken(res.data.token)
        const finalRedirect = res.data.redirect_uri || redirectUri
        const finalState = res.data.state ?? state
        if (redirectToThirdParty(res.data.token, finalRedirect, finalState)) return
        await goToDashboard()
        return
      }
    } else {
      if (!hasCode) {
        failAndBackToLogin('该登录方式需要授权码')
        return
      }
      const flag = await userStore.OaLoginIn(code)
      if (flag) {
        if (redirectToThirdParty(userStore.token, redirectUri, state)) return
        await goToDashboard()
        return
      }
    }
  } catch (e) {
    console.error(e)
  }
  failAndBackToLogin('登录失败，3秒后跳转到登录页')
}

callback()
</script>
