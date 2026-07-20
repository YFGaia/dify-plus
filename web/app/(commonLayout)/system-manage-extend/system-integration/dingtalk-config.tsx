'use client'

import type { DingTalkConfig as DingTalkConfigType } from '@/models/system-manage-extend'
import { Switch } from '@langgenius/dify-ui/switch'
import { toast } from '@langgenius/dify-ui/toast'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getDingTalkConfig, setDingTalkConfig, testDingTalkConnection } from '@/service/system-manage-extend'

type DingTalkFieldKey = 'corp_id' | 'agent_id' | 'app_key' | 'app_secret'

const getErrorMessage = (error: unknown, fallback: string) => {
  if (error instanceof Error && error.message)
    return error.message

  return fallback
}

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
    catch (error) {
      toast.error(getErrorMessage(error, 'Failed to load config'))
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
      toast.success(t(($) => $['systemManage.common.saveSuccess'], { ns: 'extend' }))
      fetchConfig()
    }
    catch (error) {
      toast.error(getErrorMessage(error, t(($) => $['systemManage.common.saveFailed'], { ns: 'extend' })))
    }
    finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    try {
      setTesting(true)
      await testDingTalkConnection()
      toast.success(t(($) => $['systemManage.common.testSuccess'], { ns: 'extend' }))
    }
    catch (error) {
      toast.error(getErrorMessage(error, t(($) => $['systemManage.common.testFailed'], { ns: 'extend' })))
    }
    finally {
      setTesting(false)
    }
  }

  const fields: Array<{ key: DingTalkFieldKey, label: string, type?: 'password' }> = [
    { key: 'corp_id', label: t(($) => $['systemManage.dingtalk.corpId'], { ns: 'extend' }) },
    { key: 'agent_id', label: t(($) => $['systemManage.dingtalk.agentId'], { ns: 'extend' }) },
    { key: 'app_key', label: t(($) => $['systemManage.dingtalk.appKey'], { ns: 'extend' }) },
    { key: 'app_secret', label: t(($) => $['systemManage.dingtalk.appSecret'], { ns: 'extend' }), type: 'password' },
  ]

  if (loading)
    return <div className="text-text-tertiary">{t(($) => $['systemManage.common.loading'], { ns: 'extend' })}</div>

  return (
    <div className="max-w-[640px] space-y-6">
      {/* 启用状态 */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.common.enable'], { ns: 'extend' })}
        </span>
        <div className="flex items-center gap-3">
          <span className={`text-xs font-medium ${config.status ? 'text-text-accent' : 'text-text-tertiary'}`}>
            {t(($) => (config.status ? $['systemManage.common.enabled'] : $['systemManage.common.disabled']), { ns: 'extend' })}
          </span>
          <Switch
            checked={config.status}
            onCheckedChange={(status: boolean) => setConfig(prev => ({ ...prev, status }))}
            aria-label={t(($) => $['systemManage.common.enable'], { ns: 'extend' })}
          />
        </div>
      </div>

      {/* 表单字段 */}
      {fields.map(field => (
        <div key={field.key} className="space-y-1">
          <label className="text-sm font-medium text-text-secondary">{field.label}</label>
          <input
            type={field.type ?? 'text'}
            value={config[field.key] || ''}
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
          {saving ? t(($) => $['systemManage.common.saving'], { ns: 'extend' }) : t(($) => $['systemManage.common.save'], { ns: 'extend' })}
        </button>
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

export default DingTalkConfig
