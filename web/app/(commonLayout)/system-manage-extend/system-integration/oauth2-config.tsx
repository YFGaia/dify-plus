'use client'

import type { OAuth2Config as OAuth2ConfigType } from '@/models/system-manage-extend'
import { Switch } from '@langgenius/dify-ui/switch'
import { toast } from '@langgenius/dify-ui/toast'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  getOAuth2Config,
  setOAuth2Config,
  testOAuth2Connection,
} from '@/service/system-manage-extend'

type OAuth2ConfigFieldKey = keyof OAuth2ConfigType['config']

const getErrorMessage = (error: unknown, fallback: string) => {
  if (error instanceof Error && error.message) return error.message

  return fallback
}

const OAuth2Config = () => {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [config, setConfig] = useState<OAuth2ConfigType>({
    status: false,
    app_id: '',
    app_secret: '',
    config: {
      server_url: '',
      authorize_url: '',
      token_url: '',
      userinfo_url: '',
      scope: 'openid email profile',
      button_text: 'SSO 登录',
      logout_url: '',
      redirect_uri: '',
    },
  })

  const fetchConfig = useCallback(async () => {
    try {
      setLoading(true)
      const data = await getOAuth2Config()
      setConfig(data)
    } catch (error) {
      toast.error(getErrorMessage(error, 'Failed to load config'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchConfig()
  }, [fetchConfig])

  const handleSave = async () => {
    try {
      setSaving(true)
      await setOAuth2Config(config)
      toast.success(t(($) => $['systemManage.common.saveSuccess'], { ns: 'extend' }))
      fetchConfig()
    } catch (error) {
      toast.error(
        getErrorMessage(
          error,
          t(($) => $['systemManage.common.saveFailed'], { ns: 'extend' }),
        ),
      )
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    try {
      setTesting(true)
      await testOAuth2Connection(config)
      toast.success(t(($) => $['systemManage.common.testSuccess'], { ns: 'extend' }))
    } catch (error) {
      toast.error(
        getErrorMessage(
          error,
          t(($) => $['systemManage.common.testFailed'], { ns: 'extend' }),
        ),
      )
    } finally {
      setTesting(false)
    }
  }

  const updateConfig = (key: OAuth2ConfigFieldKey, value: string) => {
    setConfig({ ...config, config: { ...config.config, [key]: value } })
  }

  const fields: Array<{ key: OAuth2ConfigFieldKey; label: string }> = [
    { key: 'server_url', label: t(($) => $['systemManage.oauth2.serverUrl'], { ns: 'extend' }) },
    {
      key: 'authorize_url',
      label: t(($) => $['systemManage.oauth2.authorizeUrl'], { ns: 'extend' }),
    },
    { key: 'token_url', label: t(($) => $['systemManage.oauth2.tokenUrl'], { ns: 'extend' }) },
    {
      key: 'userinfo_url',
      label: t(($) => $['systemManage.oauth2.userinfoUrl'], { ns: 'extend' }),
    },
    { key: 'scope', label: t(($) => $['systemManage.oauth2.scope'], { ns: 'extend' }) },
    { key: 'button_text', label: t(($) => $['systemManage.oauth2.buttonText'], { ns: 'extend' }) },
    { key: 'logout_url', label: t(($) => $['systemManage.oauth2.logoutUrl'], { ns: 'extend' }) },
    {
      key: 'redirect_uri',
      label: t(($) => $['systemManage.oauth2.redirectUri'], { ns: 'extend' }),
    },
  ]

  if (loading)
    return (
      <div className="text-text-tertiary">
        {t(($) => $['systemManage.common.loading'], { ns: 'extend' })}
      </div>
    )

  return (
    <div className="max-w-[640px] space-y-6">
      {/* 启用状态 */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.common.enable'], { ns: 'extend' })}
        </span>
        <div className="flex items-center gap-3">
          <span
            className={`text-xs font-medium ${config.status ? 'text-text-accent' : 'text-text-tertiary'}`}
          >
            {t(
              ($) =>
                config.status
                  ? $['systemManage.common.enabled']
                  : $['systemManage.common.disabled'],
              { ns: 'extend' },
            )}
          </span>
          <Switch
            checked={config.status}
            onCheckedChange={(status: boolean) => setConfig((prev) => ({ ...prev, status }))}
            aria-label={t(($) => $['systemManage.common.enable'], { ns: 'extend' })}
          />
        </div>
      </div>

      {/* 顶层字段 */}
      <div className="space-y-1">
        <label className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.oauth2.clientId'], { ns: 'extend' })}
        </label>
        <input
          type="text"
          value={config.app_id || ''}
          onChange={(e) => setConfig({ ...config, app_id: e.target.value })}
          className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
        />
      </div>
      <div className="space-y-1">
        <label className="text-sm font-medium text-text-secondary">
          {t(($) => $['systemManage.oauth2.clientSecret'], { ns: 'extend' })}
        </label>
        <input
          type="password"
          value={config.app_secret || ''}
          onChange={(e) => setConfig({ ...config, app_secret: e.target.value })}
          className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
        />
      </div>

      {/* Config 字段 */}
      {fields.map((field) => (
        <div key={field.key} className="space-y-1">
          <label className="text-sm font-medium text-text-secondary">{field.label}</label>
          <input
            type="text"
            value={config.config?.[field.key] || ''}
            onChange={(e) => updateConfig(field.key, e.target.value)}
            className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
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
          {saving
            ? t(($) => $['systemManage.common.saving'], { ns: 'extend' })
            : t(($) => $['systemManage.common.save'], { ns: 'extend' })}
        </button>
        <button
          onClick={handleTest}
          disabled={testing}
          className="rounded-lg border border-components-button-secondary-border bg-components-button-secondary-bg px-4 py-2 text-sm font-medium text-components-button-secondary-text hover:bg-components-button-secondary-bg-hover disabled:opacity-50"
        >
          {testing
            ? t(($) => $['systemManage.common.testing'], { ns: 'extend' })
            : t(($) => $['systemManage.common.test'], { ns: 'extend' })}
        </button>
      </div>
    </div>
  )
}

export default OAuth2Config
