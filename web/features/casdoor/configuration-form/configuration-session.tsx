import type {
  CasdoorConfigurationResponse,
  CasdoorStaticValidationResponse,
  CasdoorTestLoginResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { DraftErrors } from './configuration-draft'
import {
  zCasdoorDisableResponse,
  zCasdoorStaticValidationResponse,
  zCasdoorTestLoginResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@langgenius/dify-ui/alert-dialog'
import { Button } from '@langgenius/dify-ui/button'
import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { CallbackReference } from './callback-reference'
import { CertificateFields } from './certificate-fields'
import {
  initialConfiguration,
  parseServerConfiguration,
  validateConfiguration,
} from './configuration-draft'
import { ConnectionFields } from './connection-fields'
import { ManagementError } from './management-error'
import { ProfileFields } from './profile-fields'
import { SavedStatus } from './saved-status'
import { WorkspaceMappings } from './workspace-mappings'
import { WorkspaceSelector } from './workspace-selector'

export function ConfigurationSession({
  response,
  onRefresh,
  refreshing,
  refreshError,
}: {
  response: CasdoorConfigurationResponse & { etag: number }
  onRefresh: () => void
  refreshing: boolean
  refreshError: unknown
}) {
  const { t } = useTranslation('extend')
  const [baseline, setBaseline] = useState<CasdoorConfigurationResponse & { etag: number }>(
    response,
  )
  const [configuration, setConfiguration] = useState(() => initialConfiguration(response))
  const [certificateIds, setCertificateIds] = useState<string[]>(() =>
    (initialConfiguration(response).certificates ?? []).map(() => crypto.randomUUID()),
  )
  const [mappingIds, setMappingIds] = useState<string[]>(() =>
    (initialConfiguration(response).workspace_mappings ?? []).map(() => crypto.randomUUID()),
  )
  const [secret, setSecret] = useState('')
  const [errors, setErrors] = useState<DraftErrors>({})
  const [notice, setNotice] = useState<
    'saved' | 'cleared' | 'disabled' | 'activated' | 'secretReset' | null
  >(null)
  const [reconciliationRequired, setReconciliationRequired] = useState(false)
  const [requestError, setRequestError] = useState<unknown>(null)
  const [staticResult, setStaticResult] = useState<CasdoorStaticValidationResponse | null>(null)
  const [testLoginResult, setTestLoginResult] = useState<CasdoorTestLoginResponse | null>(null)
  const [confirmation, setConfirmation] = useState<
    'clear' | 'disable' | 'activate' | 'reedit' | null
  >(null)
  const [writeUnconfirmed, setWriteUnconfirmed] = useState(false)
  const casdoor = consoleQuery.systemManageExtend.integration.casdoor
  const dirty =
    secret !== '' ||
    JSON.stringify(configuration) !== JSON.stringify(initialConfiguration(baseline))
  const latest = response.etag > baseline.etag ? response : baseline
  const stale = latest.etag !== baseline.etag

  const acceptSaved = (data: CasdoorConfigurationResponse, message: 'saved' | 'cleared') => {
    setSecret('')
    const parsed = parseServerConfiguration(data)
    if (!parsed?.draft || parsed.etag <= baseline.etag) {
      setWriteUnconfirmed(true)
      setRequestError({})
      setConfirmation(null)
      return
    }
    setBaseline(parsed)
    setConfiguration(initialConfiguration(parsed))
    setCertificateIds(
      (parsed.draft.configuration.certificates ?? []).map(() => crypto.randomUUID()),
    )
    setMappingIds(
      (parsed.draft.configuration.workspace_mappings ?? []).map(() => crypto.randomUUID()),
    )
    setErrors({})
    setStaticResult(null)
    setTestLoginResult(null)
    setRequestError(null)
    setNotice(message)
    setConfirmation(null)
    setWriteUnconfirmed(false)
  }
  const fail = (error: unknown) => {
    setSecret('')
    setNotice('secretReset')
    setWriteUnconfirmed(true)
    setRequestError(error)
    setConfirmation(null)
  }
  const save = useMutation(
    casdoor.put.mutationOptions({
      context: { silent: true },
      onSuccess: (data) => acceptSaved(data, 'saved'),
      onError: fail,
    }),
  )
  const clear = useMutation(
    casdoor.clearSecret.post.mutationOptions({
      context: { silent: true },
      onSuccess: (data) => acceptSaved(data, 'cleared'),
      onError: fail,
    }),
  )
  const disable = useMutation(
    casdoor.disable.post.mutationOptions({
      context: { silent: true },
      onSuccess: (data, variables) => {
        const result = zCasdoorDisableResponse.safeParse(data)
        const parsed = result.success ? parseServerConfiguration(data.configuration) : null
        if (!parsed || parsed.enabled !== false || parsed.etag <= variables.body.etag) {
          fail({})
          return
        }
        setSecret('')
        setBaseline(parsed)
        setConfiguration(initialConfiguration(parsed))
        setCertificateIds(
          (initialConfiguration(parsed).certificates ?? []).map(() => crypto.randomUUID()),
        )
        setMappingIds(
          (initialConfiguration(parsed).workspace_mappings ?? []).map(() => crypto.randomUUID()),
        )
        setErrors({})
        setStaticResult(null)
        setTestLoginResult(null)
        setRequestError(null)
        setNotice('disabled')
        setReconciliationRequired(data.reconciliation_required)
        setConfirmation(null)
        setWriteUnconfirmed(false)
      },
      onError: fail,
    }),
  )
  const activate = useMutation(
    casdoor.activate.post.mutationOptions({
      context: { silent: true },
      onSuccess: (data, variables) => {
        const parsed = parseServerConfiguration(data)
        if (
          !parsed ||
          parsed.enabled !== true ||
          parsed.etag <= variables.body.etag ||
          parsed.active_revision_id !== variables.body.revision_id ||
          parsed.draft_revision_id !== variables.body.revision_id
        ) {
          fail({})
          return
        }
        setBaseline(parsed)
        setConfiguration(initialConfiguration(parsed))
        setSecret('')
        setStaticResult(null)
        setTestLoginResult(null)
        setRequestError(null)
        setNotice('activated')
        setConfirmation(null)
        setWriteUnconfirmed(false)
      },
      onError: fail,
    }),
  )
  const testLogin = useMutation(
    casdoor.testLogin.post.mutationOptions({
      context: { silent: true },
      onSuccess: (data) => {
        const parsed = zCasdoorTestLoginResponse.safeParse(data)
        if (!parsed.success) {
          setTestLoginResult(null)
          setRequestError({})
          return
        }
        setRequestError(null)
        setTestLoginResult(parsed.data)
      },
      onError: (error) => setRequestError(error),
    }),
  )
  const validate = useMutation(
    casdoor.validate.post.mutationOptions({
      context: { silent: true },
      onSuccess: (data) => {
        const parsed = zCasdoorStaticValidationResponse.safeParse(data)
        if (
          !parsed.success ||
          data.kind !== 'static' ||
          data.static_only !== true ||
          data.status !== 'passed' ||
          parsed.data.etag !== baseline.etag ||
          parsed.data.revision_id !== baseline.draft_revision_id
        ) {
          setStaticResult(null)
          setRequestError({})
          return
        }
        setStaticResult(parsed.data)
        setRequestError(null)
      },
      onError: (error) => setRequestError(error),
    }),
  )
  const busy =
    save.isPending ||
    clear.isPending ||
    disable.isPending ||
    validate.isPending ||
    activate.isPending ||
    testLogin.isPending
  const exactDraft =
    baseline.draft && baseline.draft_revision_id === baseline.draft.revision_id
      ? baseline.draft
      : null
  const actionsUnavailable =
    busy || dirty || stale || writeUnconfirmed || refreshing || Boolean(refreshError) || !exactDraft
  const reedit = () => {
    setBaseline(latest)
    setConfiguration(initialConfiguration(latest))
    setCertificateIds(
      (initialConfiguration(latest).certificates ?? []).map(() => crypto.randomUUID()),
    )
    setMappingIds(
      (initialConfiguration(latest).workspace_mappings ?? []).map(() => crypto.randomUUID()),
    )
    setSecret('')
    setErrors({})
    setRequestError(null)
    setNotice(null)
    setStaticResult(null)
    setTestLoginResult(null)
    setConfirmation(null)
    setWriteUnconfirmed(false)
    save.reset()
    clear.reset()
    disable.reset()
    validate.reset()
    activate.reset()
    testLogin.reset()
  }
  return (
    <div className="space-y-4">
      <SavedStatus response={latest} />
      <form
        noValidate
        className="space-y-6"
        onSubmit={(event) => {
          event.preventDefault()
          if (busy || stale || writeUnconfirmed || refreshing || refreshError) return
          const nextErrors = validateConfiguration(configuration)
          setErrors(nextErrors)
          if (Object.keys(nextErrors).length) return
          setRequestError(null)
          setNotice(null)
          save.mutate(
            { body: { configuration, etag: baseline.etag, ...(secret === '' ? {} : { secret }) } },
            { onSettled: () => save.reset() },
          )
        }}
      >
        <fieldset disabled={busy} className="space-y-6">
          <ConnectionFields
            configuration={configuration}
            onChange={setConfiguration}
            secret={secret}
            onSecretChange={setSecret}
            errors={errors}
          />
          <CertificateFields
            certificates={configuration.certificates ?? []}
            rowIds={certificateIds}
            onChange={(certificates, ids) => {
              setConfiguration({ ...configuration, certificates })
              setCertificateIds(ids)
            }}
            errors={errors}
          />
          <CallbackReference />
          <WorkspaceSelector
            name="default_workspace_id"
            label={t(($) => $['systemManage.casdoor.defaultWorkspace'])}
            value={configuration.default_workspace_id}
            onChange={(default_workspace_id) =>
              setConfiguration({ ...configuration, default_workspace_id })
            }
            invalid={Boolean(errors.default_workspace_id)}
            showHistory
          />
          <p className="text-sm text-text-secondary">
            {t(($) => $['systemManage.casdoor.fallback'])}
          </p>
          <WorkspaceMappings
            mappings={configuration.workspace_mappings ?? []}
            rowIds={mappingIds}
            onChange={(workspace_mappings, ids) => {
              setConfiguration({ ...configuration, workspace_mappings })
              setMappingIds(ids)
            }}
            errors={errors}
          />
          <ProfileFields configuration={configuration} onChange={setConfiguration} />
        </fieldset>
        {dirty && <p role="status">{t(($) => $['systemManage.casdoor.dirty'])}</p>}
        {stale && <p role="status">{t(($) => $['systemManage.casdoor.stale'])}</p>}
        {writeUnconfirmed && <p role="status">{t(($) => $['systemManage.casdoor.conflict'])}</p>}
        {Boolean(requestError) && <ManagementError error={requestError} />}
        {Boolean(refreshError) && <ManagementError error={refreshError} />}
        {notice && (
          <p role="status">
            {notice === 'saved'
              ? t(($) => $['systemManage.casdoor.saved'])
              : notice === 'cleared'
                ? t(($) => $['systemManage.casdoor.cleared'])
                : notice === 'disabled'
                  ? t(($) => $['systemManage.casdoor.disabled'])
                  : notice === 'activated'
                    ? t(($) => $['systemManage.casdoor.activated'])
                    : t(($) => $['systemManage.casdoor.secretReset'])}
          </p>
        )}
        {reconciliationRequired && (
          <p role="alert">{t(($) => $['systemManage.casdoor.disableReconciliation'])}</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            type="submit"
            variant="primary"
            loading={save.isPending}
            disabled={
              stale ||
              writeUnconfirmed ||
              refreshing ||
              Boolean(refreshError) ||
              (busy && !save.isPending)
            }
          >
            {t(($) => $['systemManage.casdoor.save'])}
          </Button>
          <Button type="button" loading={refreshing} disabled={busy} onClick={onRefresh}>
            {t(($) => $['systemManage.casdoor.refresh'])}
          </Button>
          <Button
            type="button"
            disabled={busy || refreshing || Boolean(refreshError)}
            onClick={() => {
              if (dirty) setConfirmation('reedit')
              else reedit()
            }}
          >
            {t(($) => $['systemManage.casdoor.reedit'])}
          </Button>
          <Button
            type="button"
            loading={validate.isPending}
            disabled={actionsUnavailable}
            onClick={() => {
              if (!exactDraft || actionsUnavailable) return
              validate.mutate({
                body: { etag: baseline.etag, revision_id: exactDraft.revision_id },
              })
            }}
          >
            {t(($) => $['systemManage.casdoor.staticCheck'])}
          </Button>
          <Button
            type="button"
            loading={clear.isPending}
            disabled={actionsUnavailable || !exactDraft?.secret_configured}
            onClick={() => setConfirmation('clear')}
          >
            {t(($) => $['systemManage.casdoor.clearSecret'])}
          </Button>
          <Button
            type="button"
            loading={testLogin.isPending}
            disabled={actionsUnavailable}
            onClick={() => {
              if (!exactDraft || actionsUnavailable) return
              setRequestError(null)
              setTestLoginResult(null)
              testLogin.mutate({
                body: { etag: baseline.etag, revision_id: exactDraft.revision_id },
              })
            }}
          >
            {t(($) => $['systemManage.casdoor.testLogin'])}
          </Button>
          {(!latest.enabled || latest.active_revision_id !== latest.draft_revision_id) && (
            <Button
              type="button"
              loading={activate.isPending}
              disabled={actionsUnavailable}
              onClick={() => setConfirmation('activate')}
            >
              {t(($) => $['systemManage.casdoor.activate'])}
            </Button>
          )}
          {latest.enabled && (
            <Button
              type="button"
              loading={disable.isPending}
              disabled={busy || stale || writeUnconfirmed || refreshing || Boolean(refreshError)}
              onClick={() => setConfirmation('disable')}
            >
              {t(($) => $['systemManage.casdoor.disable'])}
            </Button>
          )}
        </div>
      </form>
      {testLoginResult && (
        <p role="status">
          {testLoginResult.reason === 'deployment_proof_missing'
            ? t(($) => $['systemManage.casdoor.deploymentProofMissing'])
            : t(($) => $['systemManage.casdoor.liveTestNotWired'])}
        </p>
      )}
      {staticResult && (
        <div role="status" className="space-y-1">
          <p>
            {t(($) => $['systemManage.casdoor.staticPassed'], {
              id: staticResult.revision_id,
              etag: staticResult.etag,
              time: staticResult.checked_at,
            })}
          </p>
          {(staticResult.revision_id !== latest.draft_revision_id ||
            staticResult.etag !== latest.etag) && (
            <p>{t(($) => $['systemManage.casdoor.staticExpired'])}</p>
          )}
        </div>
      )}
      <AlertDialog
        open={confirmation !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setConfirmation(null)
        }}
      >
        <AlertDialogContent>
          <div className="space-y-2 p-6">
            <AlertDialogTitle>
              {confirmation === 'clear'
                ? t(($) => $['systemManage.casdoor.clearTitle'])
                : confirmation === 'disable'
                  ? t(($) => $['systemManage.casdoor.disableTitle'])
                  : confirmation === 'activate'
                    ? t(($) => $['systemManage.casdoor.activateTitle'])
                    : t(($) => $['systemManage.casdoor.reeditTitle'])}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmation === 'clear'
                ? t(($) => $['systemManage.casdoor.clearHelp'])
                : confirmation === 'disable'
                  ? t(($) => $['systemManage.casdoor.disableHelp'])
                  : confirmation === 'activate'
                    ? t(($) => $['systemManage.casdoor.activateHelp'])
                    : t(($) => $['systemManage.casdoor.reeditHelp'])}
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton type="button" disabled={busy}>
              {t(($) => $['systemManage.casdoor.cancel'])}
            </AlertDialogCancelButton>
            <AlertDialogConfirmButton
              type="button"
              loading={clear.isPending || disable.isPending || activate.isPending}
              disabled={
                (confirmation === 'clear' && actionsUnavailable) ||
                (confirmation === 'disable' &&
                  (busy || stale || writeUnconfirmed || refreshing || Boolean(refreshError))) ||
                (confirmation === 'activate' && actionsUnavailable)
              }
              onClick={() => {
                if (busy) return
                if (confirmation === 'reedit') {
                  if (refreshing || refreshError) return
                  reedit()
                  return
                }
                if (confirmation === 'disable') {
                  if (stale || writeUnconfirmed || refreshing || refreshError) return
                  disable.mutate(
                    { body: { etag: baseline.etag } },
                    { onSettled: () => disable.reset() },
                  )
                  return
                }
                if (confirmation === 'activate') {
                  if (!exactDraft || actionsUnavailable) return
                  activate.mutate(
                    { body: { etag: baseline.etag, revision_id: exactDraft.revision_id } },
                    { onSettled: () => activate.reset() },
                  )
                  return
                }
                if (!exactDraft || actionsUnavailable) return
                clear.mutate(
                  { body: { etag: baseline.etag, revision_id: exactDraft.revision_id } },
                  { onSettled: () => clear.reset() },
                )
              }}
            >
              {t(($) => $['systemManage.casdoor.confirm'])}
            </AlertDialogConfirmButton>
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
