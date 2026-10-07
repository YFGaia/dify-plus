'use client'

import type { CodeExecutionControlItem } from '@/contract/console/system-manage'
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@langgenius/dify-ui/alert-dialog'
import { toast } from '@langgenius/dify-ui/toast'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { consoleQuery } from '@/service/console'
import {
  useAddCodeExecutionControlEmail,
  useRemoveCodeExecutionControlEmail,
} from '@/service/use-system-manage'

const EMAIL_PATTERN = /^[^\s@]+@[^\s@][^\s.@]*\.[^\s@]+$/

const getErrorMessage = (e: unknown): string | undefined =>
  e instanceof Error ? e.message : undefined

const CodeExecutionControlPage = () => {
  const { t } = useTranslation()

  const [email, setEmail] = useState('')
  const [inputError, setInputError] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<CodeExecutionControlItem | null>(null)

  const listQuery = useQuery(consoleQuery.systemManage.codeExecutionControlList.queryOptions())
  const addMutation = useAddCodeExecutionControlEmail()
  const removeMutation = useRemoveCodeExecutionControlEmail()

  const items = listQuery.data?.items ?? []

  const handleAdd = () => {
    const trimmed = email.trim()
    if (!EMAIL_PATTERN.test(trimmed)) {
      setInputError(t(($) => $['systemManage.codeExecutionControl.invalidEmail'], { ns: 'extend' }))
      return
    }
    addMutation.mutate(
      { body: { email: trimmed } },
      {
        onSuccess: (data) => {
          setEmail('')
          if (data.cache_synced === false)
            toast.warning(
              t(($) => $['systemManage.codeExecutionControl.cacheSyncWarning'], { ns: 'extend' }),
            )
          else
            toast.success(
              t(($) => $['systemManage.codeExecutionControl.addSuccess'], { ns: 'extend' }),
            )
        },
        onError: (e) => {
          toast.error(
            getErrorMessage(e) ||
              t(($) => $['systemManage.codeExecutionControl.addFailed'], { ns: 'extend' }),
          )
        },
      },
    )
  }

  const handleDeleteConfirm = () => {
    if (!deleteTarget) return
    removeMutation.mutate(
      { params: { id: deleteTarget.id } },
      {
        onSuccess: (data) => {
          setDeleteTarget(null)
          if (data.cache_synced === false)
            toast.warning(
              t(($) => $['systemManage.codeExecutionControl.cacheSyncWarning'], { ns: 'extend' }),
            )
          else
            toast.success(
              t(($) => $['systemManage.codeExecutionControl.deleteSuccess'], { ns: 'extend' }),
            )
        },
        onError: (e) => {
          setDeleteTarget(null)
          toast.error(
            getErrorMessage(e) ||
              t(($) => $['systemManage.codeExecutionControl.deleteFailed'], { ns: 'extend' }),
          )
        },
      },
    )
  }

  return (
    <div className="max-w-[800px]">
      <h1 className="mb-2 text-xl font-semibold text-text-primary">
        {t(($) => $['systemManage.codeExecutionControl.title'], { ns: 'extend' })}
      </h1>
      <p className="mb-6 text-sm text-text-tertiary">
        {t(($) => $['systemManage.codeExecutionControl.description'], { ns: 'extend' })}
      </p>

      {/* 添加名单 */}
      <div className="mb-4 flex items-start gap-2">
        <div className="flex-1">
          <input
            type="text"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value)
              setInputError('')
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAdd()
            }}
            placeholder={t(($) => $['systemManage.codeExecutionControl.emailPlaceholder'], {
              ns: 'extend',
            })}
            className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary placeholder:text-text-quaternary focus:outline-none"
          />
          {inputError && <p className="mt-1 text-xs text-text-warning">{inputError}</p>}
        </div>
        <button
          onClick={handleAdd}
          disabled={addMutation.isPending}
          className="rounded-lg bg-components-button-primary-bg px-4 py-2 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover disabled:opacity-60"
        >
          {addMutation.isPending
            ? t(($) => $['systemManage.common.saving'], { ns: 'extend' })
            : t(($) => $['systemManage.codeExecutionControl.add'], { ns: 'extend' })}
        </button>
      </div>

      {/* 名单表格 */}
      <div className="overflow-hidden rounded-xl border border-divider-subtle bg-background-default-subtle">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-divider-subtle bg-background-section-burn">
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.codeExecutionControl.table.email'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.codeExecutionControl.table.createdAt'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-right font-medium text-text-tertiary">
                {t(($) => $['systemManage.codeExecutionControl.table.actions'], { ns: 'extend' })}
              </th>
            </tr>
          </thead>
          <tbody>
            {listQuery.isLoading ? (
              <tr>
                <td colSpan={3} className="px-4 py-8 text-center text-text-tertiary">
                  {t(($) => $['systemManage.common.loading'], { ns: 'extend' })}
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={3} className="px-4 py-8 text-center text-text-tertiary">
                  {t(($) => $['systemManage.codeExecutionControl.empty'], { ns: 'extend' })}
                </td>
              </tr>
            ) : (
              items.map((item) => (
                <tr
                  key={item.id}
                  className="border-b border-divider-subtle last:border-0 hover:bg-background-default-hover"
                >
                  <td className="px-4 py-3 font-medium text-text-primary">{item.email}</td>
                  <td className="px-4 py-3 text-text-tertiary">
                    {new Date(item.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setDeleteTarget(item)}
                      className="text-sm text-text-destructive hover:underline"
                    >
                      {t(($) => $['systemManage.common.delete'], { ns: 'extend' })}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 删除确认 */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <div className="flex flex-col items-start gap-2 self-stretch pt-6 pr-6 pb-4 pl-6">
            <AlertDialogTitle className="w-full title-2xl-semi-bold text-text-primary">
              {t(($) => $['systemManage.codeExecutionControl.deleteConfirmTitle'], {
                ns: 'extend',
              })}
            </AlertDialogTitle>
            <AlertDialogDescription className="w-full system-md-regular wrap-break-word whitespace-pre-wrap text-text-tertiary">
              {t(($) => $['systemManage.codeExecutionControl.deleteConfirmContent'], {
                ns: 'extend',
                email: deleteTarget?.email ?? '',
              })}
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton disabled={removeMutation.isPending}>
              {t(($) => $['systemManage.common.cancel'], { ns: 'extend' })}
            </AlertDialogCancelButton>
            <AlertDialogConfirmButton
              loading={removeMutation.isPending}
              disabled={removeMutation.isPending}
              onClick={handleDeleteConfirm}
            >
              {t(($) => $['systemManage.common.confirm'], { ns: 'extend' })}
            </AlertDialogConfirmButton>
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

export default CodeExecutionControlPage
