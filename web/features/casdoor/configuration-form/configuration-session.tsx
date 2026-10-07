import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { DraftErrors } from './configuration-draft'
import {
  zCasdoorDisableResponse,
  zCasdoorStaticValidationResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { Button } from '@langgenius/dify-ui/button'
import { Field, FieldLabel } from '@langgenius/dify-ui/field'
import { Switch } from '@langgenius/dify-ui/switch'
import { useMutation } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { CallbackReference } from './callback-reference'
import {
  configurationInput,
  inheritRoleOrganization,
  initialConfiguration,
  parseServerConfiguration,
  validateConfiguration,
} from './configuration-draft'
import { ConnectionFields } from './connection-fields'
import { parseDiagnosticStart } from './diagnostic-navigation'
import { LoginTestStatus } from './login-test-status'
import { ManagementError } from './management-error'
import { safeManagementError } from './management-error-details'
import { ProfileFields } from './profile-fields'
import { WorkspaceMappings } from './workspace-mappings'

type ServerConfiguration = CasdoorConfigurationResponse & { etag: number }
type Action = 'save' | 'test' | 'toggle' | 'refresh'

export function ConfigurationSession({
  response,
  onRefresh,
  refreshing,
  refreshError,
}: {
  response: ServerConfiguration
  onRefresh: () => Promise<ServerConfiguration | null>
  refreshing: boolean
  refreshError: unknown
}) {
  const { t } = useTranslation('extend')
  const [baseline, setBaseline] = useState(response)
  const [configuration, setConfiguration] = useState(() => initialConfiguration(response))
  const [mappingIds, setMappingIds] = useState(() =>
    (initialConfiguration(response).workspace_mappings ?? []).map(() => crypto.randomUUID()),
  )
  const [secret, setSecret] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [errors, setErrors] = useState<DraftErrors>({})
  const [notice, setNotice] = useState<
    'saved' | 'disabled' | 'activated' | 'enableNeedsTest' | null
  >(null)
  const [requestError, setRequestError] = useState<unknown>(null)
  const [writeUnconfirmed, setWriteUnconfirmed] = useState(false)
  const [reconciliationRequired, setReconciliationRequired] = useState(false)
  const [action, setAction] = useState<Action | null>(null)
  const actionLockRef = useRef(false)
  const casdoor = consoleQuery.systemManageExtend.integration.casdoor
  const save = useMutation(
    casdoor.put.mutationOptions({ context: { silent: true }, retry: false, gcTime: 0 }),
  )
  const validate = useMutation(
    casdoor.validate.post.mutationOptions({ context: { silent: true }, retry: false }),
  )
  const testLogin = useMutation(
    casdoor.testLogin.post.mutationOptions({ context: { silent: true }, retry: false }),
  )
  const activate = useMutation(
    casdoor.activate.post.mutationOptions({ context: { silent: true }, retry: false }),
  )
  const disable = useMutation(
    casdoor.disable.post.mutationOptions({ context: { silent: true }, retry: false }),
  )
  const needsAutomaticDraft = baseline.draft?.configuration.schema_version !== 2
  const legacyConfiguration =
    (baseline.draft?.configuration ?? baseline.active?.configuration)?.schema_version === 1
  const dirty =
    needsAutomaticDraft ||
    secret !== '' ||
    JSON.stringify(configuration) !== JSON.stringify(initialConfiguration(baseline))
  const latest = response.etag >= baseline.etag ? response : baseline
  const stale =
    latest.etag !== baseline.etag || latest.draft_revision_id !== baseline.draft_revision_id
  const busy = action !== null
  const unavailable = stale || writeUnconfirmed || refreshing || Boolean(refreshError)

  const updateConfiguration = (next: CasdoorConfiguration) =>
    setConfiguration(inheritRoleOrganization(next))

  const acceptConfiguration = (current: ServerConfiguration) => {
    const next = initialConfiguration(current)
    setBaseline(current)
    setConfiguration(next)
    setMappingIds((next.workspace_mappings ?? []).map(() => crypto.randomUUID()))
    setSecret('')
    setErrors({})
    setWriteUnconfirmed(false)
  }

  const run = async (nextAction: Action, operation: () => Promise<void>) => {
    if (actionLockRef.current) return
    actionLockRef.current = true
    setAction(nextAction)
    setRequestError(null)
    setNotice(null)
    try {
      await operation()
    } catch (error) {
      if (safeManagementError(error).code === 'config_conflict') setWriteUnconfirmed(true)
      setRequestError(error)
    } finally {
      actionLockRef.current = false
      setAction(null)
    }
  }

  const saveConfiguration = async () => {
    const nextErrors = validateConfiguration(configuration)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length) {
      if (
        Object.keys(nextErrors).some(
          (key) =>
            !['browser_frontend_url', 'organization', 'application', 'client_id'].includes(key) &&
            !key.startsWith('workspace_mappings'),
        )
      )
        setAdvanced(true)
      return null
    }
    const payload = {
      configuration: configurationInput(configuration),
      etag: baseline.etag,
      ...(secret === '' ? {} : { secret }),
    }
    setSecret('')
    let data: CasdoorConfigurationResponse
    try {
      data = await save.mutateAsync({ body: payload })
    } catch (error) {
      setWriteUnconfirmed(true)
      throw error
    } finally {
      save.reset()
    }
    const parsed = parseServerConfiguration(data)
    if (!parsed?.draft || parsed.etag <= baseline.etag) {
      setWriteUnconfirmed(true)
      throw new Error('Configuration save could not be confirmed.')
    }
    acceptConfiguration(parsed)
    return parsed
  }

  const validateSaved = async (current: ServerConfiguration) => {
    if (!current.draft || current.draft.revision_id !== current.draft_revision_id)
      throw new Error('Configuration unavailable.')
    const data = await validate.mutateAsync({
      body: { etag: current.etag, revision_id: current.draft.revision_id },
    })
    const parsed = zCasdoorStaticValidationResponse.safeParse(data)
    if (
      !parsed.success ||
      parsed.data.kind !== 'static' ||
      parsed.data.static_only !== true ||
      parsed.data.status !== 'passed' ||
      parsed.data.etag !== current.etag ||
      parsed.data.revision_id !== current.draft.revision_id
    )
      throw new Error('Configuration check could not be confirmed.')
  }

  const enableConfiguration = async (current: ServerConfiguration) => {
    if (!current.draft || current.draft.revision_id !== current.draft_revision_id) {
      setNotice('enableNeedsTest')
      return
    }
    let data: CasdoorConfigurationResponse
    try {
      data = await activate.mutateAsync({
        body: { etag: current.etag, revision_id: current.draft.revision_id },
      })
    } catch (error) {
      if (safeManagementError(error).code === 'config_conflict') {
        const refreshed = await onRefresh()
        if (
          refreshed &&
          (refreshed.etag !== current.etag ||
            refreshed.draft_revision_id !== current.draft_revision_id)
        )
          setWriteUnconfirmed(true)
        else setNotice('enableNeedsTest')
        return
      }
      if (!safeManagementError(error).code) setWriteUnconfirmed(true)
      throw error
    }
    const parsed = parseServerConfiguration(data)
    if (
      !parsed?.enabled ||
      parsed.etag <= current.etag ||
      parsed.active_revision_id !== current.draft.revision_id ||
      parsed.draft_revision_id !== current.draft.revision_id
    ) {
      setWriteUnconfirmed(true)
      throw new Error('Enabling Casdoor could not be confirmed.')
    }
    acceptConfiguration(parsed)
    setNotice('activated')
  }

  const submit = async (testing: boolean) => {
    if (unavailable) return
    await run(testing ? 'test' : 'save', async () => {
      const savedEdits = dirty || !baseline.draft
      const current = savedEdits ? await saveConfiguration() : baseline
      if (!current) return
      if (!testing) {
        await validateSaved(current)
        if (current.enabled && current.active_revision_id !== current.draft_revision_id) {
          if (savedEdits) setNotice('enableNeedsTest')
          else await enableConfiguration(current)
        } else setNotice('saved')
        return
      }
      if (!current.draft) return
      const data = await testLogin.mutateAsync({
        body: { etag: current.etag, revision_id: current.draft.revision_id },
      })
      const parsed = parseDiagnosticStart(data)
      if (parsed.response.status === 'blocked') {
        setRequestError({ reason: parsed.response.reason })
        return
      }
      if (parsed.destination) window.location.assign(parsed.destination)
    })
  }

  const toggleEnabled = async (enabled: boolean) => {
    if (unavailable) return
    await run('toggle', async () => {
      if (enabled) {
        if (dirty || !baseline.draft || baseline.draft.revision_id !== baseline.draft_revision_id) {
          setNotice('enableNeedsTest')
          return
        }
        await enableConfiguration(baseline)
      } else {
        let data
        try {
          data = await disable.mutateAsync({ body: { etag: baseline.etag } })
        } catch (error) {
          if (!safeManagementError(error).code) setWriteUnconfirmed(true)
          throw error
        }
        const result = zCasdoorDisableResponse.safeParse(data)
        const parsed = result.success ? parseServerConfiguration(result.data.configuration) : null
        if (!parsed || parsed.enabled !== false || parsed.etag <= baseline.etag) {
          setWriteUnconfirmed(true)
          throw new Error('Disabling Casdoor could not be confirmed.')
        }
        // Turning off sign-in does not discard fields being edited.
        setBaseline(parsed)
        setReconciliationRequired(data.reconciliation_required)
        setNotice('disabled')
      }
    })
  }

  return (
    <form
      noValidate
      className="space-y-6"
      onSubmit={(event) => {
        event.preventDefault()
        void submit(false)
      }}
    >
      <Field name="casdoor-enabled" className="flex items-center justify-between">
        <FieldLabel>{t(($) => $['systemManage.common.enable'])}</FieldLabel>
        <div className="flex items-center gap-3">
          <span
            className={
              latest.enabled
                ? 'text-xs font-medium text-text-accent'
                : 'text-xs font-medium text-text-tertiary'
            }
          >
            {latest.enabled
              ? t(($) => $['systemManage.common.enabled'])
              : t(($) => $['systemManage.common.disabled'])}
          </span>
          <Switch
            checked={latest.enabled === true}
            loading={action === 'toggle'}
            disabled={unavailable || (busy && action !== 'toggle')}
            onCheckedChange={(enabled) => {
              void toggleEnabled(enabled)
            }}
          />
        </div>
      </Field>
      <fieldset disabled={busy} className="space-y-6">
        <ConnectionFields
          configuration={configuration}
          onChange={updateConfiguration}
          secret={secret}
          onSecretChange={setSecret}
          errors={errors}
        />
        <div className="space-y-1 text-sm text-text-secondary">
          <p>{t(($) => $['systemManage.casdoor.automaticVerification'])}</p>
          {legacyConfiguration && (
            <p>{t(($) => $['systemManage.casdoor.legacyVerificationHelp'])}</p>
          )}
        </div>
        <CallbackReference />
        <WorkspaceMappings
          organization={configuration.organization}
          mappings={configuration.workspace_mappings ?? []}
          rowIds={mappingIds}
          onChange={(workspace_mappings, ids) => {
            updateConfiguration({ ...configuration, workspace_mappings })
            setMappingIds(ids)
          }}
          errors={errors}
        />
        <details open={advanced} onToggle={(event) => setAdvanced(event.currentTarget.open)}>
          <summary className="cursor-pointer text-sm font-medium text-text-secondary focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-state-accent-solid">
            {t(($) => $['systemManage.casdoor.advancedMode'])}
          </summary>
          {advanced && (
            <div className="mt-4 space-y-6">
              <ConnectionFields
                configuration={configuration}
                onChange={updateConfiguration}
                secret={secret}
                onSecretChange={setSecret}
                errors={errors}
                advancedOnly
              />
              <ProfileFields configuration={configuration} onChange={updateConfiguration} />
            </div>
          )}
        </details>
      </fieldset>
      {(stale || writeUnconfirmed || Boolean(refreshError)) && (
        <div className="space-y-2">
          <p role="status">{t(($) => $['systemManage.casdoor.reloadHelp'])}</p>
          <Button
            type="button"
            loading={action === 'refresh' || refreshing}
            disabled={busy && action !== 'refresh'}
            onClick={() => {
              void run('refresh', async () => {
                const current = await onRefresh()
                if (current) acceptConfiguration(current)
              })
            }}
          >
            {t(($) => $['systemManage.casdoor.refresh'])}
          </Button>
        </div>
      )}
      {Boolean(requestError) && <ManagementError error={requestError} />}
      {Boolean(refreshError) && <ManagementError error={refreshError} />}
      {notice && (
        <p role="status">
          {notice === 'saved'
            ? t(($) => $['systemManage.casdoor.saved'])
            : notice === 'disabled'
              ? t(($) => $['systemManage.casdoor.disabled'])
              : notice === 'activated'
                ? t(($) => $['systemManage.casdoor.activated'])
                : t(($) => $['systemManage.casdoor.enableNeedsTest'])}
        </p>
      )}
      {reconciliationRequired && (
        <p role="alert">{t(($) => $['systemManage.casdoor.disableReconciliation'])}</p>
      )}
      {!dirty && !stale && !writeUnconfirmed && latest.draft && (
        <LoginTestStatus revision={latest.draft} />
      )}
      <div className="flex gap-3 pt-2">
        <Button
          type="submit"
          variant="primary"
          loading={action === 'save'}
          disabled={unavailable || (busy && action !== 'save')}
        >
          {t(($) => $['systemManage.casdoor.save'])}
        </Button>
        <Button
          type="button"
          loading={action === 'test'}
          disabled={unavailable || (busy && action !== 'test')}
          onClick={() => {
            void submit(true)
          }}
        >
          {t(($) => $['systemManage.casdoor.testLogin'])}
        </Button>
      </div>
    </form>
  )
}
