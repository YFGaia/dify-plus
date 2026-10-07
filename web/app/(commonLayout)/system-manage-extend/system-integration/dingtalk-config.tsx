'use client'

import type { DingTalkConfigResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { FormEvent } from 'react'
import { Button } from '@langgenius/dify-ui/button'
import { Input } from '@langgenius/dify-ui/input'
import { Switch } from '@langgenius/dify-ui/switch'
import { toast } from '@langgenius/dify-ui/toast'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { consoleClient } from '@/service/console'

const integration = consoleClient.systemManageExtend.integration
const asRecord = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
const asString = (value: unknown, fallback = '') => (typeof value === 'string' ? value : fallback)
const getErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback
const controlClass =
  'w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary focus-visible:outline-2 focus-visible:outline-state-accent-solid'

type LookupKey = 'url' | 'request_param_field' | 'response_email_field'

const DingTalkConfig = () => {
  const { t } = useTranslation('extend')
  const [config, setConfig] = useState<DingTalkConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testingEmail, setTestingEmail] = useState(false)
  const [headers, setHeaders] = useState('{}')
  const [bodyData, setBodyData] = useState('{}')
  const [userId, setUserId] = useState('')
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState('')

  const fetchConfig = useCallback(async () => {
    setLoading(true)
    try {
      const data = await integration.dingtalk.get()
      setConfig(data)
      const email = asRecord(data.config?.email_api)
      setHeaders(JSON.stringify(email.headers ?? {}, null, 2))
      setBodyData(JSON.stringify(email.body_data ?? {}, null, 2))
      setError('')
    } catch {
      setError(t(($) => $['systemManage.dingtalk.emailLookup.loadFailed']))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void fetchConfig()
  }, [fetchConfig])

  const email = asRecord(config?.config?.email_api)
  const authorization = asRecord(email.authorization)
  const enabled = email.enabled === true
  const updateEmail = (patch: Record<string, unknown>) => {
    setTestResult('')
    setConfig((previous) =>
      previous
        ? {
            ...previous,
            config: {
              ...previous.config,
              email_api: { ...asRecord(previous.config?.email_api), ...patch },
            },
          }
        : previous,
    )
  }
  const updateAuthorization = (patch: Record<string, unknown>) =>
    updateEmail({ authorization: { ...authorization, ...patch } })

  const buildEmailConfig = () => {
    let parsedHeaders: unknown
    let parsedBody: unknown
    try {
      parsedHeaders = JSON.parse(headers)
      parsedBody = JSON.parse(bodyData)
    } catch {
      throw new Error(t(($) => $['systemManage.dingtalk.emailLookup.validation']))
    }
    if (
      !parsedHeaders ||
      typeof parsedHeaders !== 'object' ||
      Array.isArray(parsedHeaders) ||
      !parsedBody ||
      typeof parsedBody !== 'object' ||
      Array.isArray(parsedBody)
    )
      throw new Error(t(($) => $['systemManage.dingtalk.emailLookup.validation']))
    const url = asString(email.url).trim()
    if (enabled && !/^https?:\/\/\S+$/i.test(url))
      throw new Error(t(($) => $['systemManage.dingtalk.emailLookup.validation']))
    return {
      ...email,
      enabled,
      url,
      method: asString(email.method, 'GET'),
      request_param_field: asString(email.request_param_field).trim() || 'userId',
      response_email_field: asString(email.response_email_field).trim() || 'data[0].userName',
      headers: parsedHeaders,
      body_data: parsedBody,
    }
  }

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    if (!config) return
    setError('')
    try {
      const emailConfig = buildEmailConfig()
      setSaving(true)
      await integration.dingtalk.post({
        body: { ...config, config: { ...config.config, email_api: emailConfig } },
      })
      toast.success(t(($) => $['systemManage.common.saveSuccess']))
      await fetchConfig()
    } catch (failure) {
      setError(
        getErrorMessage(
          failure,
          t(($) => $['systemManage.common.saveFailed']),
        ),
      )
    } finally {
      setSaving(false)
    }
  }

  const handleEmailTest = async () => {
    setError('')
    setTestResult('')
    try {
      const emailConfig = buildEmailConfig()
      if (!userId.trim() || !emailConfig.url)
        throw new Error(t(($) => $['systemManage.dingtalk.emailLookup.validation']))
      setTestingEmail(true)
      const result = await integration.emailApi.test.post({
        body: { config: emailConfig, user_id: userId.trim() },
      })
      if (result.result !== 'success' || !result.email)
        throw new Error(result.message || t(($) => $['systemManage.common.testFailed']))
      setTestResult(
        t(($) => $['systemManage.dingtalk.emailLookup.testSuccess'], { email: result.email }),
      )
    } catch (failure) {
      setError(
        getErrorMessage(
          failure,
          t(($) => $['systemManage.common.testFailed']),
        ),
      )
    } finally {
      setTestingEmail(false)
    }
  }

  const handleDingTalkTest = async () => {
    try {
      setTesting(true)
      const result = await integration.dingtalk.test.get()
      if (result.result !== 'success')
        throw new Error(result.message || t(($) => $['systemManage.common.testFailed']))
      toast.success(t(($) => $['systemManage.common.testSuccess']))
    } catch (failure) {
      toast.error(
        getErrorMessage(
          failure,
          t(($) => $['systemManage.common.testFailed']),
        ),
      )
    } finally {
      setTesting(false)
    }
  }

  if (loading) return <p role="status">{t(($) => $['systemManage.common.loading'])}</p>
  if (!config) return <p role="alert">{error}</p>

  const fields = [
    { key: 'corp_id', label: t(($) => $['systemManage.dingtalk.corpId']) },
    { key: 'agent_id', label: t(($) => $['systemManage.dingtalk.agentId']) },
    { key: 'app_key', label: t(($) => $['systemManage.dingtalk.appKey']) },
    { key: 'app_secret', label: t(($) => $['systemManage.dingtalk.appSecret']) },
  ] as const
  const lookupFields: Array<{ key: LookupKey; label: string; fallback?: string }> = [
    { key: 'url', label: t(($) => $['systemManage.emailApi.url']) },
    {
      key: 'request_param_field',
      label: t(($) => $['systemManage.dingtalk.emailLookup.requestParam']),
      fallback: 'userId',
    },
    {
      key: 'response_email_field',
      label: t(($) => $['systemManage.dingtalk.emailLookup.responsePath']),
      fallback: 'data[0].userName',
    },
  ]
  const authType = asString(authorization.type, 'none')

  return (
    <form onSubmit={handleSave} className="max-w-160 space-y-6">
      <fieldset disabled={saving || testingEmail} className="space-y-6">
        <div className="flex items-center justify-between">
          <label htmlFor="dingtalk-enabled" className="text-sm font-medium text-text-secondary">
            {t(($) => $['systemManage.common.enable'])}
          </label>
          <Switch
            id="dingtalk-enabled"
            disabled={saving || testingEmail}
            checked={config.status}
            onCheckedChange={(status) => setConfig({ ...config, status })}
          />
        </div>
        {fields.map((field) => (
          <div key={field.key} className="space-y-1">
            <label
              htmlFor={`dingtalk-${field.key}`}
              className="text-sm font-medium text-text-secondary"
            >
              {field.label}
            </label>
            <Input
              id={`dingtalk-${field.key}`}
              type={field.key === 'app_secret' ? 'password' : 'text'}
              value={config[field.key] ?? ''}
              onChange={(event) => setConfig({ ...config, [field.key]: event.target.value })}
            />
          </div>
        ))}

        <fieldset className="space-y-4 rounded-xl border border-divider-subtle p-4">
          <legend className="px-1 text-sm font-semibold text-text-primary">
            {t(($) => $['systemManage.dingtalk.emailLookup.title'])}
          </legend>
          <p id="email-lookup-description" className="text-sm text-text-secondary">
            {t(($) => $['systemManage.dingtalk.emailLookup.description'])}
          </p>
          <p className="text-sm text-text-tertiary">
            {t(($) => $['systemManage.dingtalk.emailLookup.fallback'])}
          </p>
          <div className="flex items-center justify-between">
            <label
              htmlFor="email-lookup-enabled"
              className="text-sm font-medium text-text-secondary"
            >
              {t(($) => $['systemManage.dingtalk.emailLookup.enable'])}
            </label>
            <Switch
              id="email-lookup-enabled"
              disabled={saving || testingEmail}
              checked={enabled}
              aria-describedby="email-lookup-description"
              onCheckedChange={(value) => updateEmail({ enabled: value })}
            />
          </div>
          {enabled && (
            <>
              {lookupFields.map((field) => (
                <div key={field.key} className="space-y-1">
                  <label
                    htmlFor={`email-lookup-${field.key}`}
                    className="text-sm font-medium text-text-secondary"
                  >
                    {field.label}
                  </label>
                  <Input
                    id={`email-lookup-${field.key}`}
                    value={asString(email[field.key], field.fallback)}
                    placeholder={
                      field.key === 'url' ? 'https://example.com/api/email' : field.fallback
                    }
                    onChange={(event) => updateEmail({ [field.key]: event.target.value })}
                  />
                </div>
              ))}
              <p className="text-xs text-text-tertiary">
                {t(($) => $['systemManage.dingtalk.emailLookup.requestHint'])}
              </p>
              <p className="text-xs text-text-tertiary">
                {t(($) => $['systemManage.dingtalk.emailLookup.responseHint'])}
              </p>
              <pre className="overflow-auto rounded-lg bg-background-section p-3 text-xs">
                {'{"data":[{"userName":"employee@example.com"}]} → data[0].userName'}
              </pre>
              <div className="space-y-1">
                <label
                  htmlFor="email-lookup-method"
                  className="text-sm font-medium text-text-secondary"
                >
                  {t(($) => $['systemManage.dingtalk.emailLookup.method'])}
                </label>
                <select
                  id="email-lookup-method"
                  className={controlClass}
                  value={asString(email.method, 'GET')}
                  onChange={(event) => updateEmail({ method: event.target.value })}
                >
                  {['GET', 'POST', 'PUT', 'DELETE'].map((method) => (
                    <option key={method}>{method}</option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <label
                  htmlFor="email-lookup-auth"
                  className="text-sm font-medium text-text-secondary"
                >
                  {t(($) => $['systemManage.dingtalk.emailLookup.auth'])}
                </label>
                <select
                  id="email-lookup-auth"
                  className={controlClass}
                  value={authType}
                  onChange={(event) => updateAuthorization({ type: event.target.value })}
                >
                  <option value="none">
                    {t(($) => $['systemManage.dingtalk.emailLookup.none'])}
                  </option>
                  <option value="bearer">Bearer Token</option>
                  <option value="basic">HTTP Basic</option>
                </select>
              </div>
              {authType === 'bearer' && (
                <div className="space-y-1">
                  <label
                    htmlFor="email-lookup-token"
                    className="text-sm font-medium text-text-secondary"
                  >
                    Bearer Token
                  </label>
                  <Input
                    id="email-lookup-token"
                    type="password"
                    value={asString(authorization.token)}
                    onChange={(event) => updateAuthorization({ token: event.target.value })}
                  />
                </div>
              )}
              {authType === 'basic' && (
                <>
                  <div className="space-y-1">
                    <label
                      htmlFor="email-lookup-username"
                      className="text-sm font-medium text-text-secondary"
                    >
                      {t(($) => $['systemManage.dingtalk.emailLookup.username'])}
                    </label>
                    <Input
                      id="email-lookup-username"
                      value={asString(authorization.username)}
                      onChange={(event) => updateAuthorization({ username: event.target.value })}
                    />
                  </div>
                  <div className="space-y-1">
                    <label
                      htmlFor="email-lookup-password"
                      className="text-sm font-medium text-text-secondary"
                    >
                      {t(($) => $['systemManage.dingtalk.emailLookup.password'])}
                    </label>
                    <Input
                      id="email-lookup-password"
                      type="password"
                      value={asString(authorization.password)}
                      onChange={(event) => updateAuthorization({ password: event.target.value })}
                    />
                  </div>
                </>
              )}
              <details>
                <summary className="cursor-pointer text-sm font-medium text-text-secondary focus-visible:outline-2 focus-visible:outline-state-accent-solid">
                  {t(($) => $['systemManage.dingtalk.emailLookup.advanced'])}
                </summary>
                <div className="mt-3 space-y-3">
                  <p className="text-xs text-text-tertiary">
                    {t(($) => $['systemManage.dingtalk.emailLookup.advancedHint'])}
                  </p>
                  <div>
                    <label
                      htmlFor="email-lookup-body-type"
                      className="text-sm font-medium text-text-secondary"
                    >
                      {t(($) => $['systemManage.dingtalk.emailLookup.bodyType'])}
                    </label>
                    <select
                      id="email-lookup-body-type"
                      className={controlClass}
                      value={asString(email.body_type, 'raw')}
                      onChange={(event) => updateEmail({ body_type: event.target.value })}
                    >
                      {['raw', 'form-data', 'x-www-form-urlencoded'].map((type) => (
                        <option key={type}>{type}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="email-lookup-headers"
                      className="text-sm font-medium text-text-secondary"
                    >
                      {t(($) => $['systemManage.dingtalk.emailLookup.headers'])}
                    </label>
                    <textarea
                      id="email-lookup-headers"
                      className={controlClass}
                      rows={4}
                      value={headers}
                      onChange={(event) => {
                        setHeaders(event.target.value)
                        setTestResult('')
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="email-lookup-body-data"
                      className="text-sm font-medium text-text-secondary"
                    >
                      {t(($) => $['systemManage.dingtalk.emailLookup.bodyData'])}
                    </label>
                    <textarea
                      id="email-lookup-body-data"
                      className={controlClass}
                      rows={5}
                      value={bodyData}
                      onChange={(event) => {
                        setBodyData(event.target.value)
                        setTestResult('')
                      }}
                    />
                  </div>
                  <pre className="overflow-auto text-xs">
                    {`raw: ${JSON.stringify({ raw: JSON.stringify({ department: 'sales' }) })}\nform-data: ${JSON.stringify({ form_data: [{ key: 'department', value: 'sales' }] })}\nx-www-form-urlencoded: ${JSON.stringify({ urlencoded: [{ key: 'department', value: 'sales' }] })}`}
                  </pre>
                </div>
              </details>
              <div className="space-y-1">
                <label
                  htmlFor="email-lookup-user-id"
                  className="text-sm font-medium text-text-secondary"
                >
                  {t(($) => $['systemManage.dingtalk.emailLookup.userId'])}
                </label>
                <Input
                  id="email-lookup-user-id"
                  value={userId}
                  onChange={(event) => {
                    setUserId(event.target.value)
                    setTestResult('')
                  }}
                />
              </div>
              <p className="text-xs text-text-tertiary">
                {t(($) => $['systemManage.dingtalk.emailLookup.testHint'])}
              </p>
              <Button
                type="button"
                loading={testingEmail}
                disabled={saving}
                onClick={handleEmailTest}
              >
                {t(($) => $['systemManage.dingtalk.emailLookup.test'])}
              </Button>
              {testResult && (
                <p role="status" className="text-sm text-text-success">
                  {testResult}
                </p>
              )}
            </>
          )}
        </fieldset>
      </fieldset>
      {error && (
        <p role="alert" className="text-sm text-text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-3">
        <Button type="submit" variant="primary" loading={saving} disabled={testingEmail}>
          {t(($) => $['systemManage.common.save'])}
        </Button>
        <Button type="button" loading={testing} disabled={saving} onClick={handleDingTalkTest}>
          {t(($) => $['systemManage.common.test'])}
        </Button>
      </div>
    </form>
  )
}

export default DingTalkConfig
