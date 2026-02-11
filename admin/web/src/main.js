import './style/element_visiable.scss'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { createApp } from 'vue'
import ElementPlus from 'element-plus'

import 'element-plus/dist/index.css'
// 引入gin-vue-admin前端初始化相关内容
import './core/gin-vue-admin'
// 引入封装的router
import router from '@/router/index'
import '@/permission'
import run from '@/core/gin-vue-admin.js'
import auth from '@/directive/auth'
import { store } from '@/pinia'
import { useUserStore } from '@/pinia/modules/user'
import App from './App.vue'
// 消除警告
import 'default-passive-events'

const app = createApp(App)
app.config.productionTip = false

app
  .use(run)
  .use(ElementPlus)
  .use(store)
  .use(auth)
  .use(router)
  .mount('#app')

// 如果当前 URL 上带有 clear_cache=true，则清空本地缓存与 Cookie
const hasClearCacheFlag = () => {
  // 主 URL query（?a=1&clear_cache=true）
  const searchParams = new URLSearchParams(window.location.search || '')
  if (searchParams.get('clear_cache') === 'true') return true

  // hash 部分 query（/#/login?redirect_uri=...&clear_cache=true）
  const hash = window.location.hash || ''
  const idx = hash.indexOf('?')
  if (idx !== -1) {
    const hashQuery = hash.substring(idx + 1)
    const hashParams = new URLSearchParams(hashQuery)
    if (hashParams.get('clear_cache') === 'true') return true
  }
  return false
}

if (hasClearCacheFlag()) {
  const userStore = useUserStore()
  // 统一使用 store 的清理逻辑：清 token、sessionStorage、localStorage 部分键、cookie 等
  userStore.ClearStorage && userStore.ClearStorage()
}

export default app
