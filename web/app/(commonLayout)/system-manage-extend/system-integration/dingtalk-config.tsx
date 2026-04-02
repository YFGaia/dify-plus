'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Toast from '@/app/components/base/toast'
import { getDingTalkConfig, setDingTalkConfig, testDingTalkConnection } from '@/service/system-manage-extend'
import type { DingTalkConfig as DingTalkConfigType } from '@/models/system-manage-extend'

const DingTalkConfig = () => {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [config, setConfig] = useState<DingTalkConfigType>({
    status: false,
    corp_id: '',
    agent_id: '',
    app_key: '',
    app_secret: '',
    config: {},
  })

  const fetchConfig = useCallback(async () => {
    try {
      setLoading(true)
      const data = await getDingTalkConfig()
      setConfig(data)
    }
    catch (e: any) {
      Toast.notify({ type: 'error', message: e.message || 'Failed to load config' })
    }
    finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchConfig()
  }, [fetchConfig])

  const handleSave = async () => {
    try {
      setSaving(true)
      await setDingTalkConfig(config)
      Toast.notify({ type: 'success', message: t('systemManage.common.saveSuccess', { ns: 'extend' }) })
      fetchConfig()
    }
    catch (e: any) {
      Toast.notify({ type: 'error', message: e.message || t('systemManage.common.saveFailed', { ns: 'extend' }) })
    }
    finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    try {
      setTesting(true)
      await testDingTalkConnection()
      Toast.notify({ type: 'success', message: t('systemManage.common.testSuccess', { ns: 'extend' }) })
    }
    catch (e: any) {
      Toast.notify({ type: 'error', message: e.message || t('systemManage.common.testFailed', { ns: 'extend' }) })
    }
    finally {
      setTesting(false)
    }
  }

  if (loading)
    return <div className="text-text-tertiary">{t('systemManage.common.loading', { ns: 'extend' })}</div>

  return (
    <div className="max-w-[640px] space-y-6">
      {/* 启用状态 */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-text-secondary">
          {t('systemManage.common.enable', { ns: 'extend' })}
        </span>
        <button
          onClick={() => setConfig({ ...config, status: !config.status })}
          className={`relative h-6 w-11 rounded-full transition-colors ${
            config.status ? 'bg-util-colors-blue-blue-500' : 'bg-components-toggle-bg'
          }`}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
              config.status ? 'translate-x-[22px]' : 'translate-x-0.5'
            }`}
          />
        </button>
      </div>

      {/* 表单字段 */}
      {([
        { key: 'corp_id', label: t('systemManage.dingtalk.corpId', { ns: 'extend' }) },
        { key: 'agent_id', label: t('systemManage.dingtalk.agentId', { ns: 'extend' }) },
        { key: 'app_key', label: t('systemManage.dingtalk.appKey', { ns: 'extend' }) },
        { key: 'app_secret', label: t('systemManage.dingtalk.appSecret', { ns: 'extend' }), type: 'password' },
      ] as const).map(field => (
        <div key={field.key} className="space-y-1">
          <label className="text-sm font-medium text-text-secondary">{field.label}</label>
          <input
            type={('type' in field && field.type) || 'text'}
            value={(config as any)[field.key] || ''}
            onChange={e => setConfig({ ...config, [field.key]: e.target.value })}
            className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:border-components-input-border-active focus:ring-1 focus:ring-components-input-border-active"
            placeholder={field.label}
          />
        </div>
      ))}

      {/* 操作按钮 */}
      <div className="flex gap-3 pt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-lg bg-components-button-primary-bg px-4 py-2 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover disabled:opacity-50"
        >
          {saving ? t('systemManage.common.saving', { ns: 'extend' }) : t('systemManage.common.save', { ns: 'extend' })}
        </button>
        <button
          onClick={handleTest}
          disabled={testing}
          className="rounded-lg border border-components-button-secondary-border bg-components-button-secondary-bg px-4 py-2 text-sm font-medium text-components-button-secondary-text hover:bg-components-button-secondary-bg-hover disabled:opacity-50"
        >
          {testing ? t('systemManage.common.testing', { ns: 'extend' }) : t('systemManage.common.test', { ns: 'extend' })}
        </button>
      </div>
    </div>
  )
}

export default DingTalkConfig
