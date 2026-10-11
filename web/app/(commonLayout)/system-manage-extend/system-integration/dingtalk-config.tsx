'use client'

import type { DingTalkConfigResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { FormEvent } from 'react'
import { Button } from '@langgenius/dify-ui/button'
import { Input } from '@langgenius/dify-ui/input'
import { Switch } from '@langgenius/dify-ui/switch'
import { toast } from '@langgenius/dify-ui/toast'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useAtomValue } from 'jotai'
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { currentWorkspaceIdAtom } from '@/context/workspace-state'
import { dingtalkErrorMessageKey } from '@/features/dingtalk/error-message-key'
import { useSystemManagementAccess } from '@/features/system-management/access'
import { useSearchParams } from '@/next/navigation'
import { consoleClient, consoleQuery } from '@/service/console'
import DingTalkRegistration from './dingtalk-registration'

const integration = consoleClient.systemManageExtend.integration
const asRecord = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
const asString = (value: unknown, fallback = '') => (typeof value === 'string' ? value : fallback)
const controlClass =
  'w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary focus-visible:outline-2 focus-visible:outline-state-accent-solid'

type LookupKey = 'url' | 'request_param_field' | 'response_email_field'

const DingTalkConfigSession = ({
  initialConfig,
  mode,
  onSaved,
}: {
  initialConfig: DingTalkConfigResponse
  mode: 'qr' | 'manual'
  onSaved: () => Promise<void>
}) => {
  const { t } = useTranslation('extend')
  const getErrorMessage = (failure: unknown, fallback: string) => {
    const validation = t(($) => $['systemManage.dingtalk.emailLookup.validation'])
    if (failure instanceof Error && failure.message === validation) return validation
    const key = dingtalkErrorMessageKey(failure)
    return key === 'systemManage.dingtalk.loginFailed' ? fallback : t(($) => $[key])
  }
  const [config, setConfig] = useState(initialConfig)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testingEmail, setTestingEmail] = useState(false)
  const [headers, setHeaders] = useState(() =>
    JSON.stringify(asRecord(initialConfig.config?.email_api).headers ?? {}, null, 2),
  )
  const [bodyData, setBodyData] = useState(() =>
    JSON.stringify(asRecord(initialConfig.config?.email_api).body_data ?? {}, null, 2),
  )
  const [userId, setUserId] = useState('')
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState('')

  const operation = consoleQuery.systemManageExtend.integration.dingtalk
  const save = useMutation(operation.post.mutationOptions({ context: { silent: true } }))
  const activate = useMutation(operation.enable.post.mutationOptions({ context: { silent: true } }))
  const deactivate = useMutation(
    operation.disable.post.mutationOptions({ context: { silent: true } }),
  )
  const diagnostic = useMutation(
    operation.diagnostics.post.mutationOptions({ context: { silent: true } }),
  )
  const [bindAccount, setBindAccount] = useState(false)
  const [diagnosticError, setDiagnosticError] = useState('')
  const [copyResult, setCopyResult] = useState<'copied' | 'failed' | null>(null)
  const busy =
    saving ||
    testingEmail ||
    testing ||
    activate.isPending ||
    deactivate.isPending ||
    diagnostic.isPending
  const connectionVerified = initialConfig.connection_verified === true
  const loginVerified = initialConfig.verified === true
  const dirty =
    ['corp_id', 'agent_id', 'app_id', 'app_key', 'app_secret', 'config'].some(
      (key) =>
        JSON.stringify(config[key as keyof DingTalkConfigResponse]) !==
        JSON.stringify(initialConfig[key as keyof DingTalkConfigResponse]),
    ) ||
    headers !== JSON.stringify(asRecord(initialConfig.config?.email_api).headers ?? {}, null, 2) ||
    bodyData !== JSON.stringify(asRecord(initialConfig.config?.email_api).body_data ?? {}, null, 2)

  const handleActivation = async () => {
    setError('')
    try {
      if (initialConfig.status) await deactivate.mutateAsync({})
      else await activate.mutateAsync({})
      await onSaved()
    } catch (failure) {
      setError(t(($) => $[dingtalkErrorMessageKey(failure)]))
    }
  }
  const handleDiagnostic = async () => {
    setDiagnosticError('')
    try {
      const response = await diagnostic.mutateAsync({ body: { bind_current_account: bindAccount } })
      window.location.assign(response.authorization_url)
    } catch (failure) {
      setDiagnosticError(t(($) => $[dingtalkErrorMessageKey(failure)]))
    }
  }
  const handleCopyCallback = async () => {
    try {
      await navigator.clipboard.writeText(initialConfig.callback_url ?? '')
      setCopyResult('copied')
    } catch {
      setCopyResult('failed')
    }
  }

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
      await save.mutateAsync({
        body: {
          expected_revision: initialConfig.revision,
          corp_id: config.corp_id,
          agent_id: config.agent_id,
          app_id: config.app_id,
          app_key: config.app_key,
          app_secret: config.app_secret,
          config: { ...config.config, email_api: emailConfig },
        },
      })
      toast.success(t(($) => $['systemManage.common.saveSuccess']))
      await onSaved()
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
      const result = await integration.emailApi.test.post(
        {
          body: { config: emailConfig, user_id: userId.trim() },
        },
        { context: { silent: true } },
      )
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
      const result = await integration.dingtalk.test.get(undefined, { context: { silent: true } })
      if (result.result !== 'success')
        throw new Error(result.message || t(($) => $['systemManage.common.testFailed']))
      toast.success(t(($) => $['systemManage.common.testSuccess']))
      await onSaved()
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
    <div className="space-y-6">
      <section
        className="space-y-3 rounded-xl border border-divider-subtle p-4"
        aria-labelledby="dingtalk-status-title"
      >
        <h2 id="dingtalk-status-title" className="text-sm font-semibold text-text-primary">
          {t(($) => $['systemManage.dingtalk.statusTitle'])}
        </h2>
        <p>
          {t(
            ($) =>
              $[
                initialConfig.secret_configured
                  ? 'systemManage.dingtalk.credentialsReady'
                  : 'systemManage.dingtalk.credentialsMissing'
              ],
          )}
        </p>
        <p>
          {t(
            ($) =>
              $[
                connectionVerified
                  ? 'systemManage.common.testSuccess'
                  : 'systemManage.dingtalk.connectionPending'
              ],
          )}
        </p>
        <p>
          {t(
            ($) =>
              $[
                loginVerified
                  ? 'systemManage.dingtalk.loginVerified'
                  : 'systemManage.dingtalk.loginRequired'
              ],
          )}
        </p>
        <p>
          {t(
            ($) =>
              $[
                initialConfig.status
                  ? 'systemManage.common.enabled'
                  : 'systemManage.common.disabled'
              ],
          )}
        </p>
        <label htmlFor="dingtalk-callback" className="block text-sm font-medium">
          {t(($) => $['systemManage.dingtalk.callbackUrl'])}
        </label>
        <div className="flex items-center gap-2">
          <Input id="dingtalk-callback" readOnly value={initialConfig.callback_url ?? ''} />
          <Button type="button" disabled={!initialConfig.callback_url} onClick={handleCopyCallback}>
            {t(($) => $['systemManage.dingtalk.copyCallback'])}
          </Button>
        </div>
        {copyResult && (
          <p
            role={copyResult === 'failed' ? 'alert' : 'status'}
            className="text-sm text-text-secondary"
          >
            {copyResult === 'failed'
              ? t(($) => $['systemManage.dingtalk.copyFailed'])
              : t(($) => $['operation.copied'], { ns: 'common' })}
          </p>
        )}
        <ol className="list-decimal space-y-1 pl-5 text-sm text-text-secondary">
          <li>{t(($) => $['systemManage.dingtalk.loginSetupHint'])}</li>
          <li>{t(($) => $['systemManage.dingtalk.permissionsHint'])}</li>
        </ol>
        <a
          href="https://open-dev.dingtalk.com/"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block text-sm text-text-accent underline focus-visible:outline-2 focus-visible:outline-state-accent-solid"
        >
          {t(($) => $['systemManage.dingtalk.developerConsole'])}
        </a>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={bindAccount}
            onChange={(event) => setBindAccount(event.target.checked)}
            disabled={busy}
          />
          {t(($) => $['systemManage.dingtalk.bindCurrentAccount'])}
        </label>
        <div className="flex flex-wrap gap-3">
          <Button
            type="button"
            loading={testing}
            disabled={(busy && !testing) || !initialConfig.secret_configured || dirty}
            onClick={handleDingTalkTest}
          >
            {t(($) => $['systemManage.common.test'])}
          </Button>
          <Button
            type="button"
            loading={diagnostic.isPending}
            disabled={(busy && !diagnostic.isPending) || !connectionVerified || dirty}
            onClick={handleDiagnostic}
          >
            {t(($) => $['systemManage.dingtalk.verifyLogin'])}
          </Button>
          <Button
            type="button"
            loading={activate.isPending || deactivate.isPending}
            aria-describedby={
              !initialConfig.status && !loginVerified ? 'dingtalk-enable-hint' : undefined
            }
            disabled={
              (busy && !(activate.isPending || deactivate.isPending)) ||
              (!initialConfig.status && (!loginVerified || dirty))
            }
            onClick={handleActivation}
          >
            {t(
              ($) =>
                $[
                  initialConfig.status
                    ? 'systemManage.dingtalk.disable'
                    : 'systemManage.common.enable'
                ],
            )}
          </Button>
        </div>
        {!initialConfig.status && !loginVerified && (
          <p id="dingtalk-enable-hint" className="text-sm text-text-secondary">
            {t(($) => $['systemManage.dingtalk.enableHint'])}
          </p>
        )}
        {dirty && (
          <p className="text-sm text-text-secondary">
            {t(($) => $['systemManage.dingtalk.saveFirst'])}
          </p>
        )}
        {diagnosticError && (
          <p role="alert" className="text-sm text-text-destructive">
            {diagnosticError}
          </p>
        )}
      </section>
      <form onSubmit={handleSave} className="space-y-6">
        <fieldset disabled={busy} className="space-y-6">
          {mode === 'manual' && (
            <p className="text-sm text-text-secondary">
              {t(($) => $['systemManage.dingtalk.manualHint'])}
            </p>
          )}
          {mode === 'manual' &&
            fields.map((field) => (
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
                  autoComplete={field.key === 'app_secret' ? 'new-password' : 'off'}
                  placeholder={
                    field.key === 'app_secret' && initialConfig.secret_configured
                      ? t(($) => $['systemManage.dingtalk.secretPreserved'])
                      : undefined
                  }
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
                {email.credentials_configured === true && (
                  <p className="text-sm text-text-secondary">
                    {t(($) => $['systemManage.dingtalk.secretPreserved'])}
                  </p>
                )}
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
          <Button type="submit" variant="primary" loading={saving} disabled={busy && !saving}>
            {t(($) => $['systemManage.common.save'])}
          </Button>
        </div>
      </form>
    </div>
  )
}

const DingTalkConfigOwner = ({
  workspaceId,
  canManageSystem,
}: {
  workspaceId: string
  canManageSystem: boolean
}) => {
  const { t } = useTranslation('extend')
  const [mode, setMode] = useState<'qr' | 'manual'>('qr')
  const params = useSearchParams()
  const result = params.get('dingtalk_result')
  const operation = consoleQuery.systemManageExtend.integration.dingtalk
  const query = useQuery(
    operation.get.queryOptions({
      queryKey: [...operation.get.key(), { workspaceId }],
      enabled: canManageSystem,
      retry: false,
      staleTime: 0,
      refetchOnWindowFocus: false,
      context: { silent: true },
    }),
  )
  const { refetch } = query
  const refresh = useCallback(async () => {
    await refetch()
  }, [refetch])
  if (!canManageSystem) return <p role="alert">{t(($) => $['systemManage.common.noPermission'])}</p>
  if (query.isPending) return <p role="status">{t(($) => $['systemManage.common.loading'])}</p>
  if (query.isError || !query.data)
    return <p role="alert">{t(($) => $['systemManage.dingtalk.emailLookup.loadFailed'])}</p>
  return (
    <div className="max-w-160 space-y-6">
      {result && (
        <p role={result === 'success' && query.data.verified ? 'status' : 'alert'}>
          {result === 'success' && query.data.verified
            ? t(($) => $['systemManage.dingtalk.loginVerified'])
            : t(($) => $[dingtalkErrorMessageKey(result)])}
        </p>
      )}
      <fieldset className="space-y-3">
        <legend className="text-sm font-semibold text-text-primary">
          {t(($) => $['systemManage.dingtalk.setupMethod'])}
        </legend>
        <div className="flex gap-4">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="dingtalk-setup"
              checked={mode === 'qr'}
              onChange={() => setMode('qr')}
            />
            {t(($) => $['systemManage.dingtalk.qrSetup'])}
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="dingtalk-setup"
              checked={mode === 'manual'}
              onChange={() => setMode('manual')}
            />
            {t(($) => $['systemManage.dingtalk.manualSetup'])}
          </label>
        </div>
      </fieldset>
      {mode === 'qr' && <DingTalkRegistration onComplete={refresh} />}
      <DingTalkConfigSession
        key={query.data.revision}
        initialConfig={query.data}
        mode={mode}
        onSaved={refresh}
      />
    </div>
  )
}

const DingTalkConfig = () => {
  const workspaceId = useAtomValue(currentWorkspaceIdAtom)
  const { canManageSystem } = useSystemManagementAccess()
  return (
    <DingTalkConfigOwner
      key={workspaceId}
      workspaceId={workspaceId}
      canManageSystem={canManageSystem}
    />
  )
}

export default DingTalkConfig
