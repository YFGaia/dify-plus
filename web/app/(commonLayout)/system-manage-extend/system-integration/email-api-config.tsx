'use client'

import { toast } from '@langgenius/dify-ui/toast'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { testEmailApi } from '@/service/system-manage-extend'

const EmailApiConfig = () => {
  const { t } = useTranslation()
  const [testing, setTesting] = useState(false)
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')

  const handleTest = async () => {
    if (!url.trim()) {
      toast.error('API URL is required')
      return
    }
    try {
      setTesting(true)
      const result = await testEmailApi(url, key)
      if (result.result === 'success')
        toast.success(t(($) => $['systemManage.common.testSuccess'], { ns: 'extend' }))
      else
        toast.error(result.message || t(($) => $['systemManage.common.testFailed'], { ns: 'extend' }))
    }
    catch (e: any) {
      toast.error(e.message || t(($) => $['systemManage.common.testFailed'], { ns: 'extend' }))
    }
    finally {
      setTesting(false)
    }
  }

  return (
    <div className="max-w-[640px] space-y-6">
      <p className="text-sm text-text-tertiary">
        邮箱 API 配置随钉钉配置的 config JSON 一起保存。此处仅提供连通性测试。
      </p>

      <div className="space-y-1">
        <label className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.emailApi.url'], { ns: 'extend' })}
        </label>
        <input
          type="text"
          value={url}
          onChange={e => setUrl(e.target.value)}
          className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
          placeholder="https://..."
        />
      </div>

      <div className="space-y-1">
        <label className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.emailApi.key'], { ns: 'extend' })}
        </label>
        <input
          type="password"
          value={key}
          onChange={e => setKey(e.target.value)}
          className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
          placeholder="API Key"
        />
      </div>

      <div className="pt-2">
        <button
          onClick={handleTest}
          disabled={testing}
          className="rounded-lg border border-components-button-secondary-border bg-components-button-secondary-bg px-4 py-2 text-sm font-medium text-components-button-secondary-text hover:bg-components-button-secondary-bg-hover disabled:opacity-50"
        >
          {testing ? t(($) => $['systemManage.common.testing'], { ns: 'extend' }) : t(($) => $['systemManage.common.test'], { ns: 'extend' })}
        </button>
      </div>
    </div>
  )
}

export default EmailApiConfig
