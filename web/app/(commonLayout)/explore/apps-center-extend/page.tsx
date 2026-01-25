'use client'

import React, { useEffect } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import AppListCenter from '@/app/components/explore/app-list-center-extend'

const Apps = () => {
  const router = useRouter()
  const searchParams = useSearchParams()

  useEffect(() => {
    // 处理 URL 中的 token 参数（向后兼容）
    // 新版本通过 cookie 设置 token，所以需要清理 URL 参数
    const accessToken = searchParams.get('access_token')
    const refreshToken = searchParams.get('refresh_token')
    const csrfToken = searchParams.get('csrf_token')
    const consoleToken = searchParams.get('console_token') // 旧版本兼容

    if (accessToken || refreshToken || csrfToken || consoleToken) {
      // 如果 URL 中有 token 参数，清理它们（token 已通过 cookie 设置）
      const newSearchParams = new URLSearchParams(searchParams.toString())
      newSearchParams.delete('access_token')
      newSearchParams.delete('refresh_token')
      newSearchParams.delete('csrf_token')
      newSearchParams.delete('console_token')

      const newQueryString = newSearchParams.toString()
      const newUrl = newQueryString
        ? `/explore/apps-center-extend?${newQueryString}`
        : '/explore/apps-center-extend'

      // 使用 replace 而不是 push，避免在历史记录中留下带 token 的 URL
      router.replace(newUrl)
    }
  }, [searchParams, router])

  return <AppListCenter />
}

export default React.memo(Apps)
