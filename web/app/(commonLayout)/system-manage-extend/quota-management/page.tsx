'use client'

import type { QuotaListItem } from '@/models/system-manage-extend'
import { toast } from '@langgenius/dify-ui/toast'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getQuotaList, setUserQuota } from '@/service/system-manage-extend'

const PAGE_SIZE_OPTIONS = [10, 30, 50, 100]

const getErrorMessage = (e: unknown): string | undefined =>
  e instanceof Error ? e.message : undefined

const AvatarCell = ({ item }: { item: QuotaListItem }) => {
  if (item.avatar) {
    return (
      <img
        src={item.avatar}
        alt={item.name}
        className="size-8 rounded-full object-cover"
        onError={(e) => {
          ;(e.target as HTMLImageElement).style.display = 'none'
        }}
      />
    )
  }
  return (
    <div className="flex size-8 items-center justify-center rounded-full bg-primary-100 text-xs font-semibold text-primary-600">
      {item.name?.charAt(0)?.toUpperCase() || '?'}
    </div>
  )
}

const QuotaManagementPage = () => {
  const { t } = useTranslation()

  // ── 列表状态 ──────────────────────────────────────────────
  const [list, setList] = useState<QuotaListItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [keyword, setKeyword] = useState('')
  const [inputKeyword, setInputKeyword] = useState('')
  const [loading, setLoading] = useState(true)

  // ── 编辑弹窗状态 ──────────────────────────────────────────
  const [editTarget, setEditTarget] = useState<QuotaListItem | null>(null)
  const [editValue, setEditValue] = useState('')
  const [editError, setEditError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // ── 数据加载 ──────────────────────────────────────────────
  const fetchList = useCallback(async (pg: number, ps: number, kw: string) => {
    try {
      setLoading(true)
      const data = await getQuotaList({ page: pg, page_size: ps, keyword: kw || undefined })
      setList(data.list || [])
      setTotal(data.total || 0)
      setPage(data.page || pg)
    } catch (e) {
      toast.error(getErrorMessage(e) || 'Failed to load quota list')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchList(page, pageSize, keyword)
  }, [fetchList, page, pageSize, keyword])

  // ── 搜索 ──────────────────────────────────────────────────
  const handleSearch = () => {
    setPage(1)
    setKeyword(inputKeyword)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSearch()
  }

  // ── 分页 ──────────────────────────────────────────────────
  const totalPages = Math.ceil(total / pageSize) || 1

  // ── 编辑额度 ──────────────────────────────────────────────
  const openEdit = (item: QuotaListItem) => {
    setEditTarget(item)
    setEditValue(String(item.total_quota))
    setEditError('')
    setTimeout(() => inputRef.current?.focus(), 50)
  }

  const validateEdit = (val: string) => {
    if (!/^\d+(?:\.\d+)?$/.test(val.trim()))
      return t(($) => $['systemManage.quota.editDialog.invalidInput'], { ns: 'extend' })
    if (Number.parseFloat(val) < 0)
      return t(($) => $['systemManage.quota.editDialog.invalidInput'], { ns: 'extend' })
    return ''
  }

  const handleEditConfirm = async () => {
    if (!editTarget) return
    const err = validateEdit(editValue)
    if (err) {
      setEditError(err)
      return
    }
    try {
      setSubmitting(true)
      await setUserQuota({
        account_id: editTarget.account_id,
        quota: Number.parseFloat(editValue.trim()),
      })
      toast.success(t(($) => $['systemManage.quota.editDialog.success'], { ns: 'extend' }))
      setEditTarget(null)
      fetchList(page, pageSize, keyword)
    } catch (e) {
      toast.error(
        getErrorMessage(e) || t(($) => $['systemManage.quota.editDialog.failed'], { ns: 'extend' }),
      )
    } finally {
      setSubmitting(false)
    }
  }

  const handleEditCancel = () => {
    setEditTarget(null)
  }

  // ── 渲染 ──────────────────────────────────────────────────
  return (
    <div>
      <h1 className="mb-6 text-xl font-semibold text-text-primary">
        {t(($) => $['systemManage.quota.title'], { ns: 'extend' })}
      </h1>

      {/* 搜索栏 */}
      <div className="mb-4 flex items-center gap-2">
        <input
          type="text"
          value={inputKeyword}
          onChange={(e) => setInputKeyword(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t(($) => $['systemManage.quota.search'], { ns: 'extend' })}
          className="w-64 rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary placeholder:text-text-quaternary focus:border-components-input-border-active focus:outline-none"
        />
        <button
          onClick={handleSearch}
          className="rounded-lg bg-components-button-primary-bg px-4 py-2 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover"
        >
          {t(($) => $['systemManage.quota.searchButton'], { ns: 'extend' })}
        </button>
      </div>

      {/* 表格 */}
      <div className="overflow-hidden rounded-xl border border-divider-subtle bg-background-default-subtle">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-divider-subtle bg-background-section-burn">
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.ranking'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.avatar'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.member'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-left font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.email'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-right font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.usedQuota'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-right font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.totalQuota'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-right font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.balance'], { ns: 'extend' })}
              </th>
              <th className="px-4 py-3 text-right font-medium text-text-tertiary">
                {t(($) => $['systemManage.quota.table.actions'], { ns: 'extend' })}
              </th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-text-tertiary">
                  {t(($) => $['systemManage.common.loading'], { ns: 'extend' })}
                </td>
              </tr>
            ) : list.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-text-tertiary">
                  {t(($) => $['systemManage.quota.empty'], { ns: 'extend' })}
                </td>
              </tr>
            ) : (
              list.map((item) => (
                <tr
                  key={item.account_id}
                  className="border-b border-divider-subtle last:border-0 hover:bg-background-default-hover"
                >
                  <td className="px-4 py-3 text-text-tertiary">#{item.ranking}</td>
                  <td className="px-4 py-3">
                    <AvatarCell item={item} />
                  </td>
                  <td className="px-4 py-3 font-medium text-text-primary">{item.name}</td>
                  <td className="px-4 py-3 text-text-secondary">{item.email}</td>
                  <td className="px-4 py-3 text-right text-text-secondary">
                    {item.used_quota.toFixed(4)} USD
                  </td>
                  <td className="px-4 py-3 text-right text-text-secondary">
                    {item.total_quota.toFixed(4)} USD
                  </td>
                  <td
                    className={`px-4 py-3 text-right font-medium ${item.balance < 0 ? 'text-text-warning' : 'text-text-success'}`}
                  >
                    {item.balance.toFixed(4)} USD
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => openEdit(item)}
                      className="text-sm text-text-accent hover:underline"
                    >
                      {t(($) => $['systemManage.quota.action.edit'], { ns: 'extend' })}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      {!loading && total > 0 && (
        <div className="mt-4 flex items-center justify-between text-sm text-text-tertiary">
          <div className="flex items-center gap-2">
            <span>每页</span>
            <select
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value))
                setPage(1)
              }}
              className="rounded border border-divider-subtle bg-background-default px-2 py-1 text-text-secondary"
            >
              {PAGE_SIZE_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <span>
              条，共
              {total} 条
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              className="rounded px-2 py-1 hover:bg-state-base-hover disabled:opacity-40"
            >
              ‹
            </button>
            <span className="px-2">
              第{page} /{totalPages} 页
            </span>
            <button
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="rounded px-2 py-1 hover:bg-state-base-hover disabled:opacity-40"
            >
              ›
            </button>
          </div>
        </div>
      )}

      {/* 编辑额度对话框 */}
      {editTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-[400px] rounded-xl bg-components-panel-bg p-6 shadow-xl">
            <h3 className="mb-4 text-base font-semibold text-text-primary">
              {t(($) => $['systemManage.quota.editDialog.title'], {
                ns: 'extend',
                name: editTarget.name,
              })}
            </h3>
            <input
              ref={inputRef}
              type="text"
              value={editValue}
              onChange={(e) => {
                setEditValue(e.target.value)
                setEditError('')
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleEditConfirm()
              }}
              placeholder={t(($) => $['systemManage.quota.editDialog.inputPlaceholder'], {
                ns: 'extend',
              })}
              className="w-full rounded-lg border border-components-input-border-active bg-components-input-bg-normal px-3 py-2 text-sm text-text-primary focus:outline-none"
            />
            {editError && <p className="mt-1 text-xs text-text-warning">{editError}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={handleEditCancel}
                className="rounded-lg border border-divider-subtle px-4 py-2 text-sm text-text-secondary hover:bg-state-base-hover"
              >
                {t(($) => $['systemManage.common.cancel'], { ns: 'extend' })}
              </button>
              <button
                onClick={handleEditConfirm}
                disabled={submitting}
                className="rounded-lg bg-components-button-primary-bg px-4 py-2 text-sm font-medium text-components-button-primary-text hover:bg-components-button-primary-bg-hover disabled:opacity-60"
              >
                {submitting
                  ? t(($) => $['systemManage.common.saving'], { ns: 'extend' })
                  : t(($) => $['systemManage.common.confirm'], { ns: 'extend' })}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default QuotaManagementPage
