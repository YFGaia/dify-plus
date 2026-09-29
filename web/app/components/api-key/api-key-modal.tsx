'use client'
import type { ApiKeyItem, ApiKeyQuotaPayload } from '@dify/contracts/api/console/apps/types.gen'
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
import { cn } from '@langgenius/dify-ui/cn'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@langgenius/dify-ui/dialog'
import { IconButton } from '@langgenius/dify-ui/icon-button'
import { Input } from '@langgenius/dify-ui/input'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAtomValue } from 'jotai'
import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Loading from '@/app/components/base/loading'
import DayLimitItemExtend from '@/app/components/base/param-item/day-limit-item-extend'
import MonthLimitItemExtend from '@/app/components/base/param-item/month-limit-item-extend'
import { currentWorkspaceAtom } from '@/context/workspace-state'
import { consoleQuery } from '@/service/console'
import { ApiKeyTable } from './api-key-table'
import { CreatedApiKeyDialog } from './created-api-key-dialog'
import { DatasetScopeDialog } from './dataset-scope-dialog'

type CreatedApiKey = Pick<ApiKeyItem, 'token'>
type ApiKeyScope =
  | { type: 'app'; appId: string }
  | { type: 'dataset' }
  | { type: 'environment'; appId: string; environmentId: string }

type ApiKeyModalProps = {
  open: boolean
  canManage: boolean
  scope: ApiKeyScope
  onOpenChange: (open: boolean) => void
}

function AppQuotaForm({
  apiKey,
  pending,
  failed,
  onSubmit,
}: {
  apiKey?: ApiKeyItem
  pending: boolean
  failed: boolean
  onSubmit: (body: ApiKeyQuotaPayload) => void
}) {
  const { t } = useTranslation()
  const descriptionId = useId()
  const [description, setDescription] = useState(apiKey?.description ?? '')
  const [dayLimit, setDayLimit] = useState(apiKey?.day_limit_quota ?? -1)
  const [monthLimit, setMonthLimit] = useState(apiKey?.month_limit_quota ?? -1)
  const [submitted, setSubmitted] = useState(false)
  const validLimits = [dayLimit, monthLimit].every(
    (value) => Number.isFinite(value) && (value === -1 || value >= 0),
  )

  return (
    <form
      className="flex flex-col gap-4"
      // The quota contract allows any nonnegative amount or -1. The legacy
      // controls' increment step must not impose an additional submit constraint.
      noValidate
      onSubmit={(event) => {
        event.preventDefault()
        setSubmitted(true)
        if (!pending && validLimits && description.length <= 50)
          onSubmit({ description, day_limit_quota: dayLimit, month_limit_quota: monthLimit })
      }}
    >
      <label htmlFor={descriptionId}>
        {t(($) => $['apiKeyModal.descriptionPlaceholder'], { ns: 'extend' })}
      </label>
      <Input
        id={descriptionId}
        value={description}
        maxLength={50}
        disabled={pending}
        onChange={(event) => setDescription(event.target.value)}
      />
      <DayLimitItemExtend
        value={dayLimit}
        enable={!pending}
        onChange={(_key, value) => setDayLimit(value)}
      />
      <MonthLimitItemExtend
        value={monthLimit}
        enable={!pending}
        onChange={(_key, value) => setMonthLimit(value)}
      />
      {submitted && !validLimits && (
        <p role="alert">
          {t(($) => $['systemManage.quota.editDialog.invalidInput'], { ns: 'extend' })}
          {'; '}
          {t(($) => $['apiKeyModal.noLimitTips'], { ns: 'extend' })}
        </p>
      )}
      {failed && <p role="alert">{t(($) => $['api.actionFailed'], { ns: 'common' })}</p>}
      <Button type="submit" loading={pending}>
        {apiKey
          ? t(($) => $['operation.save'], { ns: 'common' })
          : t(($) => $['operation.create'], { ns: 'common' })}
      </Button>
    </form>
  )
}

export function ApiKeyModal({ open, canManage, scope, onOpenChange }: ApiKeyModalProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [quotaEditor, setQuotaEditor] = useState<{ appId: string; apiKey?: ApiKeyItem }>()
  const [quotaEditorOpen, setQuotaEditorOpen] = useState(false)
  const currentWorkspace = useAtomValue(currentWorkspaceAtom)
  const [deleteKeyId, setDeleteKeyId] = useState<string>()
  const [createdApiKey, setCreatedApiKey] = useState<CreatedApiKey>()
  const [scopeDialogOpen, setScopeDialogOpen] = useState(false)
  // Bumped on each open so the scope dialog remounts with fresh selection state.
  const [scopeDialogKey, setScopeDialogKey] = useState(0)

  const appApiKeysQuery = useQuery(
    consoleQuery.apps.byResourceId.apiKeys.get.queryOptions({
      input: open && scope.type === 'app' ? { params: { resource_id: scope.appId } } : skipToken,
    }),
  )
  const datasetApiKeysQuery = useQuery({
    ...consoleQuery.datasets.apiKeys.get.queryOptions(),
    enabled: open && scope.type === 'dataset',
  })
  const environmentApiKeysQuery = useQuery(
    consoleQuery.enterprise.appDeploy.accessService.listEnvironmentApiKeys.queryOptions({
      input:
        open && scope.type === 'environment'
          ? { params: { app_id: scope.appId, environment_id: scope.environmentId } }
          : skipToken,
    }),
  )

  const createAppApiKey = useMutation(consoleQuery.apps.byResourceId.apiKeys.post.mutationOptions())
  const updateAppApiKey = useMutation(
    consoleQuery.apps.byResourceId.apiKeys.put.mutationOptions({
      onSuccess: (_data, variables) =>
        queryClient.invalidateQueries({
          queryKey: consoleQuery.apps.byResourceId.apiKeys.get.queryKey({
            input: { params: { resource_id: variables.params.resource_id } },
          }),
        }),
    }),
  )
  const deleteAppApiKey = useMutation(
    consoleQuery.apps.byResourceId.apiKeys.byApiKeyId.delete.mutationOptions(),
  )
  const createDatasetApiKey = useMutation(consoleQuery.datasets.apiKeys.post.mutationOptions())
  const deleteDatasetApiKey = useMutation(
    consoleQuery.datasets.apiKeys.byApiKeyId.delete.mutationOptions(),
  )
  const createEnvironmentApiKey = useMutation(
    consoleQuery.enterprise.appDeploy.accessService.createEnvironmentApiKey.mutationOptions(),
  )
  const deleteEnvironmentApiKey = useMutation(
    consoleQuery.enterprise.appDeploy.accessService.deleteEnvironmentApiKey.mutationOptions(),
  )

  const apiKeys =
    scope.type === 'app'
      ? appApiKeysQuery.data?.data
      : scope.type === 'dataset'
        ? datasetApiKeysQuery.data?.data
        : environmentApiKeysQuery.data?.data
  const isLoading =
    scope.type === 'app'
      ? appApiKeysQuery.isLoading
      : scope.type === 'dataset'
        ? datasetApiKeysQuery.isLoading
        : environmentApiKeysQuery.isLoading
  const isCreating =
    scope.type === 'app'
      ? createAppApiKey.isPending
      : scope.type === 'dataset'
        ? createDatasetApiKey.isPending
        : createEnvironmentApiKey.isPending
  const isDeleting =
    scope.type === 'app'
      ? deleteAppApiKey.isPending
      : scope.type === 'dataset'
        ? deleteDatasetApiKey.isPending
        : deleteEnvironmentApiKey.isPending
  const createDisabled = !currentWorkspace.id || !canManage

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setDeleteKeyId(undefined)
      setCreatedApiKey(undefined)
      setScopeDialogOpen(false)
      setQuotaEditorOpen(false)
    }
    onOpenChange(nextOpen)
  }

  const handleCreate = () => {
    if (createDisabled || isCreating) return

    switch (scope.type) {
      case 'app':
        createAppApiKey.reset()
        updateAppApiKey.reset()
        setQuotaEditor({ appId: scope.appId })
        setQuotaEditorOpen(true)
        break
      case 'dataset':
        // Dataset keys pick a knowledge-base scope before creation; the scope dialog
        // owns that step and calls handleCreateDatasetKey with the chosen ids.
        setScopeDialogKey((key) => key + 1)
        setScopeDialogOpen(true)
        break
      case 'environment':
        createEnvironmentApiKey.mutate(
          { params: { app_id: scope.appId, environment_id: scope.environmentId } },
          { onSuccess: setCreatedApiKey },
        )
        break
    }
  }

  const handleSubmitQuota = (body: ApiKeyQuotaPayload) => {
    if (
      scope.type !== 'app' ||
      quotaEditor?.appId !== scope.appId ||
      createDisabled ||
      createAppApiKey.isPending ||
      updateAppApiKey.isPending
    )
      return
    if (quotaEditor.apiKey) {
      updateAppApiKey.mutate(
        {
          params: { resource_id: scope.appId },
          body: { ...body, id: quotaEditor.apiKey.id },
        },
        { onSuccess: () => setQuotaEditorOpen(false) },
      )
    } else {
      createAppApiKey.mutate(
        { params: { resource_id: scope.appId }, body },
        {
          onSuccess: (apiKey) => {
            setQuotaEditorOpen(false)
            setCreatedApiKey(apiKey)
          },
        },
      )
    }
  }

  const handleCreateDatasetKey = (datasetIds: string[]) => {
    if (createDatasetApiKey.isPending) return
    createDatasetApiKey.mutate(
      { body: { dataset_ids: datasetIds } },
      {
        onSuccess: (apiKey) => {
          setCreatedApiKey(apiKey)
          setScopeDialogOpen(false)
        },
      },
    )
  }

  const handleDelete = () => {
    if (!deleteKeyId || isDeleting) return

    const onSuccess = () => setDeleteKeyId(undefined)
    switch (scope.type) {
      case 'app':
        deleteAppApiKey.mutate(
          { params: { resource_id: scope.appId, api_key_id: deleteKeyId } },
          { onSuccess },
        )
        break
      case 'dataset':
        deleteDatasetApiKey.mutate({ params: { api_key_id: deleteKeyId } }, { onSuccess })
        break
      case 'environment':
        deleteEnvironmentApiKey.mutate(
          {
            params: {
              app_id: scope.appId,
              environment_id: scope.environmentId,
              api_key_id: deleteKeyId,
            },
          },
          { onSuccess },
        )
        break
    }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          className={cn(
            'flex w-200 flex-col overflow-hidden p-0',
            scope.type === 'app' && 'w-[90vw] max-w-300',
          )}
        >
          <div className="flex shrink-0 flex-col gap-1 px-6 pt-6 pr-14 pb-4">
            <DialogTitle className="title-2xl-semi-bold text-text-primary">
              {t(($) => $['apiKeyModal.apiSecretKey'], { ns: 'appApi' })}
            </DialogTitle>
            <DialogDescription className="system-sm-regular text-text-tertiary">
              {t(($) => $['apiKeyModal.apiSecretKeyTips'], { ns: 'appApi' })}
            </DialogDescription>
          </div>
          <DialogClose
            render={
              <IconButton
                aria-label={t(($) => $['operation.close'], { ns: 'common' })}
                size="lg"
                className="absolute inset-e-6 top-6"
              >
                <span aria-hidden className="i-ri-close-line size-4" />
              </IconButton>
            }
          />
          {isLoading && (
            <div className="flex min-h-24 items-center border-y border-divider-subtle px-6">
              <Loading />
            </div>
          )}
          {!!apiKeys?.length && (
            <ApiKeyTable
              apiKeys={apiKeys}
              canManage={canManage}
              showScope={scope.type === 'dataset'}
              showQuota={scope.type === 'app'}
              onEditRequest={
                scope.type === 'app'
                  ? (apiKey) => {
                      createAppApiKey.reset()
                      updateAppApiKey.reset()
                      setQuotaEditor({ appId: scope.appId, apiKey })
                      setQuotaEditorOpen(true)
                    }
                  : undefined
              }
              onDeleteRequest={setDeleteKeyId}
            />
          )}
          <div className="flex shrink-0 px-6 py-4">
            <Button disabled={createDisabled} loading={isCreating} onClick={handleCreate}>
              <span aria-hidden className="i-ri-add-line size-4 shrink-0" />
              {t(($) => $['apiKeyModal.createNewSecretKey'], { ns: 'appApi' })}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      <Dialog
        open={open && scope.type === 'app' && quotaEditor?.appId === scope.appId && quotaEditorOpen}
        onOpenChange={(nextOpen) => {
          if (!createAppApiKey.isPending && !updateAppApiKey.isPending) setQuotaEditorOpen(nextOpen)
        }}
      >
        <DialogContent className="flex flex-col gap-4">
          <DialogTitle>{t(($) => $['apiKeyModal.apiSecretKey'], { ns: 'appApi' })}</DialogTitle>
          <DialogDescription>
            {t(($) => $['apiKeyModal.noLimitTips'], { ns: 'extend' })}
          </DialogDescription>
          <DialogClose
            render={
              <IconButton
                aria-label={t(($) => $['operation.close'], { ns: 'common' })}
                disabled={createAppApiKey.isPending || updateAppApiKey.isPending}
                className="absolute top-4 right-4"
              >
                <span aria-hidden className="i-ri-close-line size-4" />
              </IconButton>
            }
          />
          <AppQuotaForm
            key={`${quotaEditor?.appId}:${quotaEditor?.apiKey?.id ?? 'new'}`}
            apiKey={quotaEditor?.apiKey}
            pending={createAppApiKey.isPending || updateAppApiKey.isPending}
            failed={createAppApiKey.isError || updateAppApiKey.isError}
            onSubmit={handleSubmitQuota}
          />
        </DialogContent>
      </Dialog>
      <AlertDialog
        open={deleteKeyId !== undefined}
        onOpenChange={(open) => {
          if (!open && !isDeleting) setDeleteKeyId(undefined)
        }}
      >
        <AlertDialogContent>
          <div className="flex flex-col gap-2 px-6 pt-6 pb-4">
            <AlertDialogTitle className="w-full truncate title-2xl-semi-bold text-text-primary">
              {t(($) => $['actionMsg.deleteConfirmTitle'], { ns: 'appApi' })}
            </AlertDialogTitle>
            <AlertDialogDescription className="w-full system-md-regular wrap-break-word whitespace-pre-wrap text-text-tertiary">
              {t(($) => $['actionMsg.deleteConfirmTips'], { ns: 'appApi' })}
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton disabled={isDeleting}>
              {t(($) => $['operation.cancel'], { ns: 'common' })}
            </AlertDialogCancelButton>
            <AlertDialogConfirmButton loading={isDeleting} onClick={handleDelete}>
              {t(($) => $['operation.confirm'], { ns: 'common' })}
            </AlertDialogConfirmButton>
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
      <CreatedApiKeyDialog
        open={open && createdApiKey !== undefined}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setCreatedApiKey(undefined)
        }}
        value={createdApiKey?.token ?? ''}
      />
      {scope.type === 'dataset' && (
        <DatasetScopeDialog
          key={scopeDialogKey}
          open={scopeDialogOpen}
          isCreating={createDatasetApiKey.isPending}
          onOpenChange={setScopeDialogOpen}
          onConfirm={handleCreateDatasetKey}
        />
      )}
    </>
  )
}
