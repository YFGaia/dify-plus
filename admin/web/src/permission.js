import { useUserStore } from '@/pinia/modules/user'
import { useRouterStore } from '@/pinia/modules/router'
import getPageTitle from '@/utils/page'
import router from '@/router'
import Nprogress from 'nprogress'
import 'nprogress/nprogress.css'
Nprogress.configure({ showSpinner: false, ease: 'ease', speed: 500 })

const whiteList = ['Login', 'Init', 'LoginCallback'] // 新增OA登录：LoginCallback

// 检测当前 URL 是否带有 OAuth/钉钉回调参数（code 或 authCode + state），用于处理回调时 hash 被丢弃或已有 token 被误跳到 defaultRouter 的情况
const hasOAuthCallbackInUrl = () => {
  const search = window.location.search
  if (!search) return null
  const params = new URLSearchParams(search)
  const code = params.get('code') || params.get('authCode')
  const state = params.get('state')
  if (!code || !state) return null
  if (state === 'dingtalk') return 'dingtalk'
  if (state === 'oauth2') return 'oauth2'
  return null
}

const getRouter = async(userStore) => {
  const routerStore = useRouterStore()
  await routerStore.SetAsyncRouter()
  await userStore.GetUserInfo()
  const asyncRouters = routerStore.asyncRouters
  asyncRouters.forEach(asyncRouter => {
    router.addRoute(asyncRouter)
  })
}

const removeLoading = () => {
  const element = document.getElementById('gva-loading-box');
  if (element) {
    element.remove();
  }
}


async function handleKeepAlive(to) {
  if (to.matched.some(item => item.meta.keepAlive)) {
    if (to.matched && to.matched.length > 2) {
      for (let i = 1; i < to.matched.length; i++) {
        const element = to.matched[i - 1]
        if (element.name === 'layout') {
          to.matched.splice(i, 1)
          await handleKeepAlive(to)
        }
        // 如果没有按需加载完成则等待加载
        if (typeof element.components.default === 'function') {
          await element.components.default()
          await handleKeepAlive(to)
        }
      }
    }
  }
}

router.beforeEach(async(to, from) => {
  const routerStore = useRouterStore()
  Nprogress.start()
  const userStore = useUserStore()
  to.meta.matched = [...to.matched]
  handleKeepAlive(to)
  const token = userStore.token

  // 钉钉/OAuth 回调：若重定向后 hash 被丢弃，URL 仅有 ?code=xx&state=dingtalk，则强制进入 loginCallback 处理
  const oauthProvider = hasOAuthCallbackInUrl()
  if (oauthProvider && to.name !== 'LoginCallback') {
    return { path: '/loginCallback', query: { provider: oauthProvider } }
  }

  // 在白名单中的判断情况
  document.title = getPageTitle(to.meta.title, to)
  if(to.meta.client) {
    return true
  }
  if (whiteList.indexOf(to.name) > -1) {
    if (token) {
      if (!routerStore.asyncRouterFlag && whiteList.indexOf(from.name) < 0) {
        await getRouter(userStore)
      }
      // 正在进行 OAuth/钉钉回调（URL 带 code）时，不跳到 defaultRouter，留在 LoginCallback 处理
      if (oauthProvider) {
        return true
      }
      // token 可以解析但是却是不存在的用户 id 或角色 id 会导致无限调用
      if (userStore.userInfo?.authority?.defaultRouter != null) {
        if (router.hasRoute(userStore.userInfo.authority.defaultRouter)) {
          return { name: userStore.userInfo.authority.defaultRouter }
        } else {
          return { path: '/layout/404' }
        }
      } else {
        // 强制退出账号
        userStore.ClearStorage()
        return {
          name: 'Login',
          query: {
            redirect: document.location.hash
          }
        }
      }
    } else {
      return true
    }
  } else {
    // 不在白名单中并且已经登录的时候
    if (token) {
      if(sessionStorage.getItem("needToHome") === 'true') {
        sessionStorage.removeItem("needToHome")
        return { path: '/'}
      }
      // 添加flag防止多次获取动态路由和栈溢出
      if (!routerStore.asyncRouterFlag && whiteList.indexOf(from.name) < 0) {
        await getRouter(userStore)
        if (userStore.token) {
          if (router.hasRoute(userStore.userInfo.authority.defaultRouter)) {
            return { ...to, replace: true }
          } else {
            return { path: '/layout/404' }
          }
        } else {
          return {
            name: 'Login',
            query: { redirect: to.href }
          }
        }
      } else {
        if (to.matched.length) {
          return true
        } else {
          return { path: '/layout/404' }
        }
      }
    }
    // 不在白名单中并且未登录的时候
    if (!token) {
      return {
        name: 'Login',
        query: {
          redirect: document.location.hash
        }
      }
    }
  }
})


router.afterEach(() => {
  // 路由加载完成后关闭进度条
  document.getElementsByClassName('main-cont main-right')[0]?.scrollTo(0, 0)
  Nprogress.done()
})

router.onError(() => {
  // 路由发生错误后销毁进度条
  Nprogress.remove()
})

removeLoading()
