import { API_PREFIX } from '@/config'
import { fetchWithRetry } from '@/utils'
import { isClient } from '@/utils/client'

const LOCAL_STORAGE_KEY = 'is_other_tab_refreshing'

let isRefreshing = false
function waitUntilTokenRefreshed() {
  return new Promise<void>((resolve) => {
    function _check() {
      const isRefreshingSign = globalThis.localStorage.getItem(LOCAL_STORAGE_KEY)
      if ((isRefreshingSign && isRefreshingSign === '1') || isRefreshing) {
        setTimeout(() => {
          _check()
        }, 1000)
      } else {
        resolve()
      }
    }
    _check()
  })
}

const isRefreshingSignAvailable = function (delta: number) {
  const nowTime = new Date().getTime()
  const lastTime = globalThis.localStorage.getItem('last_refresh_time') || '0'
  return nowTime - Number.parseInt(lastTime) <= delta
}

function isPrivateCasdoorIdentityRequest(request?: Request) {
  try {
    if (!(request instanceof Request) || request.method !== 'GET') return false
    const prefix = new URL(API_PREFIX, window.location.origin)
    const target = new URL(request.url)
    return (
      target.origin === prefix.origin &&
      target.pathname === `${prefix.pathname.replace(/\/$/, '')}/account/casdoor-identity`
    )
  } catch {
    return false
  }
}

// only one request can send
async function getNewAccessToken(timeout: number, privateCasdoorIdentity = false): Promise<void> {
  try {
    const isRefreshingSign = globalThis.localStorage.getItem(LOCAL_STORAGE_KEY)
    if (
      (isRefreshingSign && isRefreshingSign === '1' && isRefreshingSignAvailable(timeout)) ||
      isRefreshing
    ) {
      await waitUntilTokenRefreshed()
    } else {
      isRefreshing = true
      globalThis.localStorage.setItem(LOCAL_STORAGE_KEY, '1')
      globalThis.localStorage.setItem('last_refresh_time', new Date().getTime().toString())
      globalThis.addEventListener('beforeunload', releaseRefreshLock)

      // Do not use baseFetch to refresh tokens.
      // If a 401 response occurs and baseFetch itself attempts to refresh the token,
      // it can lead to an infinite loop if the refresh attempt also returns 401.
      // To avoid this, handle token refresh separately in a dedicated function
      // that does not call baseFetch and uses a single retry mechanism.
      const [error, ret] = await fetchWithRetry(
        globalThis.fetch(`${API_PREFIX}/refresh-token`, {
          method: 'POST',
          credentials: 'include', // Important: include cookies in the request
          headers: {
            'Content-Type': 'application/json;utf-8',
          },
          // No body needed - refresh token is in cookie
        }),
      )
      if (error) {
        if (privateCasdoorIdentity) throw error
        return Promise.reject(error)
      } else {
        if (ret.status === 401) {
          if (privateCasdoorIdentity) throw ret
          return Promise.reject(ret)
        }
      }
    }
  } catch (error) {
    // Throwing avoids an orphan rejection if finally also fails to release the lock.
    if (privateCasdoorIdentity) throw error
    console.error(error)
    return Promise.reject(error)
  } finally {
    releaseRefreshLock()
  }
}

function releaseRefreshLock() {
  // Always clear the refresh lock to avoid cross-tab deadlocks.
  // This is safe to call multiple times and from tabs that were only waiting.
  isRefreshing = false
  globalThis.localStorage.removeItem(LOCAL_STORAGE_KEY)
  globalThis.localStorage.removeItem('last_refresh_time')
  globalThis.removeEventListener('beforeunload', releaseRefreshLock)
}

export async function refreshAccessTokenOrReLogin(timeout: number, originatingRequest?: Request) {
  if (!isClient) return Promise.reject(new Error('refresh token is client-only'))
  const privateCasdoorIdentity = isPrivateCasdoorIdentityRequest(originatingRequest)

  return Promise.race([
    new Promise<void>((resolve, reject) =>
      setTimeout(() => {
        if (privateCasdoorIdentity) {
          try {
            releaseRefreshLock()
          } catch (error) {
            reject(error)
            return
          }
          reject(new Error('request timeout'))
          return
        }
        releaseRefreshLock()
        reject(new Error('request timeout'))
      }, timeout),
    ),
    getNewAccessToken(timeout, privateCasdoorIdentity),
  ])
}
