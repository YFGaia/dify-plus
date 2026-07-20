'use client'

import type { ForwardToken } from '@/models/system-manage-extend'
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
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  createForwardToken,
  deleteForwardToken,
  getForwardTokens,
} from '@/service/system-manage-extend'

const getErrorMessage = (e: unknown): string | undefined =>
  e instanceof Error ? e.message : undefined

const ForwardTokenList = () => {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(true)
  const [tokens, setTokens] = useState<ForwardToken[]>([])
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ForwardToken | null>(null)
  const [deleting, setDeleting] = useState(false)

  const fetchTokens = useCallback(async () => {
    try {
      setLoading(true)
      const data = await getForwardTokens()
      setTokens(data.tokens || [])
    } catch (e) {
      toast.error(getErrorMessage(e) || 'Failed to load tokens')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchTokens()
  }, [fetchTokens])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      setCreating(true)
      await createForwardToken(newName.trim())
      toast.success(t(($) => $['systemManage.common.saveSuccess'], { ns: 'extend' }))
      setNewName('')
      setShowAdd(false)
      fetchTokens()
    } catch (e) {
      toast.error(getErrorMessage(e) || 'Failed to create token')
    } finally {
      setCreating(false)
    }
  }

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return
    try {
      setDeleting(true)
      await deleteForwardToken(deleteTarget.seq)
      toast.success('Token deleted')
      setDeleteTarget(null)
      fetchTokens()
    } catch (e) {
      toast.error(getErrorMessage(e) || 'Failed to delete token')
    } finally {
      setDeleting(false)
    }
  }

  if (loading)
    return (
      <div className="text-text-tertiary">
        {t(($) => $['systemManage.common.loading'], { ns: 'extend' })}
      </div>
    )

  return (
    <div className="max-w-[800px]">
      {/* 新增按钮 */}
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-base font-medium text-text-primary">
          {t(($) => $['systemManage.forwardToken.title'], { ns: 'extend' })}
        </h3>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="rounded-lg bg-components-button-primary-bg px-3 py-1.5 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover"
        >
          {t(($) => $['systemManage.forwardToken.addToken'], { ns: 'extend' })}
        </button>
      </div>

      {/* 新增表单 */}
      {showAdd && (
        <div className="mb-4 flex items-center gap-3 rounded-lg border border-divider-subtle bg-background-default p-3">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={t(($) => $['systemManage.forwardToken.enterName'], { ns: 'extend' })}
            className="flex-1 rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-1.5 text-sm text-text-primary outline-none focus:ring-1 focus:ring-components-input-border-active"
            onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
          />
          <button
            onClick={handleCreate}
            disabled={creating || !newName.trim()}
            className="rounded-lg bg-components-button-primary-bg px-3 py-1.5 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover disabled:opacity-50"
          >
            {t(($) => $['systemManage.common.create'], { ns: 'extend' })}
          </button>
          <button
            onClick={() => {
              setShowAdd(false)
              setNewName('')
            }}
            className="rounded-lg border border-components-button-secondary-border px-3 py-1.5 text-sm text-text-secondary hover:bg-state-base-hover"
          >
            {t(($) => $['systemManage.common.cancel'], { ns: 'extend' })}
          </button>
        </div>
      )}

      {/* Token 列表 */}
      {tokens.length === 0 ? (
        <div className="py-12 text-center text-sm text-text-tertiary">
          {t(($) => $['systemManage.forwardToken.empty'], { ns: 'extend' })}
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-divider-subtle">
          <table className="w-full">
            <thead>
              <tr className="border-b border-divider-subtle bg-background-default-subtle">
                <th className="px-4 py-2.5 text-left text-xs font-medium text-text-tertiary">
                  {t(($) => $['systemManage.forwardToken.name'], { ns: 'extend' })}
                </th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-text-tertiary">
                  {t(($) => $['systemManage.forwardToken.token'], { ns: 'extend' })}
                </th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-text-tertiary">
                  {t(($) => $['systemManage.forwardToken.createdAt'], { ns: 'extend' })}
                </th>
                <th className="px-4 py-2.5 text-right text-xs font-medium text-text-tertiary">
                  {t(($) => $['systemManage.forwardToken.actions'], { ns: 'extend' })}
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr
                  key={token.seq}
                  className="border-b border-divider-subtle last:border-b-0 hover:bg-state-base-hover"
                >
                  <td className="px-4 py-3 text-sm text-text-primary">{token.name}</td>
                  <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                    {token.token.substring(0, 16)}
                    ...
                  </td>
                  <td className="px-4 py-3 text-sm text-text-tertiary">
                    {new Date(token.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setDeleteTarget(token)}
                      className="text-sm text-text-destructive hover:text-text-destructive-secondary"
                    >
                      {t(($) => $['systemManage.common.delete'], { ns: 'extend' })}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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
              {t(($) => $['systemManage.common.delete'], { ns: 'extend' })}
            </AlertDialogTitle>
            <AlertDialogDescription className="w-full system-md-regular wrap-break-word whitespace-pre-wrap text-text-tertiary">
              {t(($) => $['systemManage.forwardToken.deleteConfirm'], { ns: 'extend' })}
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton disabled={deleting}>
              {t(($) => $['systemManage.common.cancel'], { ns: 'extend' })}
            </AlertDialogCancelButton>
            <AlertDialogConfirmButton
              loading={deleting}
              disabled={deleting}
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

export default ForwardTokenList
