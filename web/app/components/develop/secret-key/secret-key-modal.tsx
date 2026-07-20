'use client'
// 二开部分 - 密钥额度类型
import type {
  ApiKeyItemResponse,
  ApikeyItemResponseWithQuotaLimitExtend,
  CreateApiKeyResponse,
} from '@/models/app'
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
import { Dialog, DialogContent, DialogTitle } from '@langgenius/dify-ui/dialog'
import { useAtomValue } from 'jotai'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import ActionButton from '@/app/components/base/action-button'
import CopyFeedback from '@/app/components/base/copy-feedback'
import Loading from '@/app/components/base/loading'
import { currentWorkspaceAtom, isCurrentWorkspaceManagerAtom } from '@/context/workspace-state'
import useTimestamp from '@/hooks/use-timestamp'
import {
  createApikey as createAppApikey,
  delApikey as delAppApikey,
  editApikey, // 二开部分 - 密钥额度
} from '@/service/apps'
import {
  createApikey as createDatasetApikey,
  delApikey as delDatasetApikey,
} from '@/service/datasets'
import { useDatasetApiKeys, useInvalidateDatasetApiKeys } from '@/service/knowledge/use-dataset'
import { useAppApiKeys, useInvalidateAppApiKeys } from '@/service/use-apps'
import SecretKeyGenerateModal from './secret-key-generate'
// 二开部分Start - 密钥额度
import SecretKeyQuotaSetExtendModal from './secret-key-quota-set-modal-extend'
// 二开部分End - 密钥额度
import s from './style.module.css'

type ISecretKeyModalProps = {
  isShow: boolean
  appId?: string
  canManage: boolean
  onClose: () => void
}

const SecretKeyModal = ({ isShow = false, appId, canManage, onClose }: ISecretKeyModalProps) => {
  const { t } = useTranslation()
  const { formatTime } = useTimestamp()
  const currentWorkspace = useAtomValue(currentWorkspaceAtom)
  // 二开部分 - 密钥额度限制编辑入口需要管理员判断（上游 1.16.0 删除 app-context，改用 workspace-state 原子）
  const isCurrentWorkspaceManager = useAtomValue(isCurrentWorkspaceManagerAtom)
  const [showConfirmDelete, setShowConfirmDelete] = useState(false)
  const [isVisible, setIsVisible] = useState(false)
  const [newKey, setNewKey] = useState<CreateApiKeyResponse | undefined>(undefined)
  const invalidateAppApiKeys = useInvalidateAppApiKeys()
  const invalidateDatasetApiKeys = useInvalidateDatasetApiKeys()
  const { data: appApiKeys, isLoading: isAppApiKeysLoading } = useAppApiKeys(appId, {
    enabled: !!appId && isShow,
  })
  const { data: datasetApiKeys, isLoading: isDatasetApiKeysLoading } = useDatasetApiKeys({
    enabled: !appId && isShow,
  })
  const apiKeysList = appId ? appApiKeys : datasetApiKeys
  const isApiKeysLoading = appId ? isAppApiKeysLoading : isDatasetApiKeysLoading

  // ---------------------- 二开部分Begin - 密钥额度 ----------------------
  const [isVisibleExtend, setVisibleExtend] = useState(false)
  const [keyItem, setKeyItem] = useState<ApikeyItemResponseWithQuotaLimitExtend>({
    created_at: '',
    id: '',
    last_used_at: '',
    token: '',
    description: '',
    day_limit_quota: -1,
    month_limit_quota: -1,
  })
  // 打开新增密钥额度编辑框
  const openSecretKeyQuotaSetModalExtend = async () => {
    setVisibleExtend(true)
    setKeyItem({
      created_at: '',
      id: '',
      last_used_at: '',
      token: '',
      description: '',
      day_limit_quota: -1,
      month_limit_quota: -1,
    })
  }
  // 打开编辑密钥额度编辑框
  const openSecretKeyQuotaEditModalExtend = async (api: ApiKeyItemResponse) => {
    setVisibleExtend(true)
    setKeyItem({
      created_at: api.created_at,
      id: api.id,
      last_used_at: api.last_used_at,
      token: api.token,
      description: api.description,
      day_limit_quota: api.day_limit_quota,
      month_limit_quota: api.month_limit_quota,
    })
  }
  // 设置密钥额度数据
  const handleSetKeyDataSetQuotas = (newKeyItems: ApikeyItemResponseWithQuotaLimitExtend) => {
    setKeyItem(newKeyItems)
  }

  // 密钥额度限制编辑
  const onEdit = async () => {
    const params = {
      url: `/apps/${appId}/api-keys`,
      body: {
        id: keyItem.id,
        description: keyItem.description,
        day_limit_quota: keyItem.day_limit_quota,
        month_limit_quota: keyItem.month_limit_quota,
      },
    }
    const res = await editApikey(params)
    setVisibleExtend(false)
    setNewKey(res)
    if (appId) invalidateAppApiKeys(appId)
    else invalidateDatasetApiKeys()
  }
  // ---------------------- 二开部分End - 密钥额度 ----------------------

  const [delKeyID, setDelKeyId] = useState('')

  const onDel = async () => {
    setShowConfirmDelete(false)
    if (!canManage) return
    if (!delKeyID) return

    const delApikey = appId ? delAppApikey : delDatasetApikey
    const params = appId
      ? { url: `/apps/${appId}/api-keys/${delKeyID}`, params: {} }
      : { url: `/datasets/api-keys/${delKeyID}`, params: {} }
    await delApikey(params)
    if (appId) invalidateAppApiKeys(appId)
    else invalidateDatasetApiKeys()
  }

  // 二开部分 - 密钥额度限制：创建密钥时携带额度参数，并关闭额度设置弹窗
  const onCreate = async () => {
    if (!currentWorkspace.id || !canManage) return

    const body = {
      description: keyItem.description,
      day_limit_quota: keyItem.day_limit_quota,
      month_limit_quota: keyItem.month_limit_quota,
    }
    const params = appId
      ? { url: `/apps/${appId}/api-keys`, body }
      : { url: '/datasets/api-keys', body }
    const createApikey = appId ? createAppApikey : createDatasetApikey
    const res = await createApikey(params)
    setVisibleExtend(false) // 关闭额度设置弹窗
    setIsVisible(true)
    setNewKey(res)
    if (appId) invalidateAppApiKeys(appId)
    else invalidateDatasetApiKeys()
  }

  const generateToken = (token: string) => {
    return `${token.slice(0, 3)}...${token.slice(-20)}`
  }

  const handleDeleteConfirmOpenChange = (open: boolean) => {
    if (open) return

    setDelKeyId('')
    setShowConfirmDelete(false)
  }

  const handleClose = () => {
    setIsVisible(false)
    onClose()
  }

  return (
    <>
      <Dialog
        open={isShow}
        onOpenChange={(open) => {
          if (!open) handleClose()
        }}
      >
        <DialogContent
          className={cn(
            'max-h-[calc(100vh-80px)]! w-full max-w-[800px]! overflow-hidden! border-none text-left align-middle',
            `${s.customModal} flex flex-col px-8`,
          )}
        >
          <DialogTitle className="title-2xl-semi-bold text-text-primary">
            {`${t(($) => $['apiKeyModal.apiSecretKey'], { ns: 'appApi' })}`}
          </DialogTitle>

          <div className="-mt-6 -mr-2 mb-4 flex justify-end">
            <button
              type="button"
              aria-label={t(($) => $['operation.close'], { ns: 'common' })}
              className="flex size-6 cursor-pointer items-center justify-center text-text-tertiary"
              onClick={handleClose}
            >
              <span
                className="i-heroicons-x-mark-20-solid size-6 cursor-pointer"
                aria-hidden="true"
              />
            </button>
          </div>
          <p className="mt-1 shrink-0 text-[13px] leading-5 font-normal text-text-tertiary">
            {t(($) => $['apiKeyModal.apiSecretKeyTips'], { ns: 'appApi' })}
          </p>
          {isApiKeysLoading && (
            <div className="mt-4">
              <Loading />
            </div>
          )}
          {!!apiKeysList?.data?.length && (
            <div className="mt-4 flex grow flex-col overflow-hidden">
              {/* 表头和行放在同一个 overflow-auto 容器，保证横向滚动同步 */}
              <div className="grow overflow-auto">
                {/* 表头：sticky top-0 使其垂直滚动时不离开视口，同时跟随横向滚动 */}
                <div className="sticky top-0 z-10 flex h-9 shrink-0 items-center border-b border-divider-regular bg-components-panel-bg text-xs font-semibold text-text-tertiary">
                  <div className="w-52 shrink-0 px-3">
                    {t(($) => $['apiKeyModal.secretKey'], { ns: 'appApi' })}
                  </div>
                  <div className="w-[155px] shrink-0 px-3">
                    {t(($) => $['apiKeyModal.created'], { ns: 'appApi' })}
                  </div>
                  <div className="w-[155px] shrink-0 px-3">
                    {t(($) => $['apiKeyModal.lastUsed'], { ns: 'appApi' })}
                  </div>
                  {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
                  <div className="w-20 shrink-0 px-3">
                    {t(($) => $['apiKeyModal.descriptionPlaceholder'], { ns: 'extend' })}
                  </div>
                  <div className="w-[135px] shrink-0 px-3">
                    {t(($) => $['apiKeyModal.dayLimit'], { ns: 'extend' })}
                  </div>
                  <div className="w-[135px] shrink-0 px-3">
                    {t(($) => $['apiKeyModal.monthLimit'], { ns: 'extend' })}
                  </div>
                  <div className="w-[110px] shrink-0 px-3">
                    {t(($) => $['apiKeyModal.accumulatedLimit'], { ns: 'extend' })}
                  </div>
                  {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
                  <div className="w-[88px] shrink-0 px-3"></div>
                </div>
                {apiKeysList.data.map((api) => (
                  <div
                    className="flex h-9 items-center border-b border-divider-regular text-sm font-normal text-text-secondary"
                    key={api.id}
                  >
                    <div className="w-52 shrink-0 truncate px-3 font-mono">
                      {generateToken(api.token)}
                    </div>
                    <div className="w-[155px] shrink-0 truncate px-3">
                      {formatTime(
                        Number(api.created_at),
                        t(($) => $.dateTimeFormat, { ns: 'appLog' }) as string,
                      )}
                    </div>
                    <div className="w-[155px] shrink-0 truncate px-3">
                      {api.last_used_at
                        ? formatTime(
                            Number(api.last_used_at),
                            t(($) => $.dateTimeFormat, { ns: 'appLog' }) as string,
                          )
                        : t(($) => $.never, { ns: 'appApi' })}
                    </div>
                    {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
                    <div className="w-20 shrink-0 truncate px-3">{api.description}</div>
                    <div className="w-[135px] shrink-0 truncate px-3">
                      ${api.day_used_quota}
                      &nbsp;/&nbsp;
                      {api.day_limit_quota === -1
                        ? t(($) => $['apiKeyModal.noLimit'], { ns: 'extend' })
                        : `$${api.day_limit_quota}`}
                    </div>
                    <div className="w-[135px] shrink-0 truncate px-3">
                      ${api.month_used_quota}
                      &nbsp;/&nbsp;
                      {api.month_limit_quota === -1
                        ? t(($) => $['apiKeyModal.noLimit'], { ns: 'extend' })
                        : `$${api.month_limit_quota}`}
                    </div>
                    <div className="w-[110px] shrink-0 truncate px-3">${api.accumulated_quota}</div>
                    {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
                    <div className="flex w-[88px] shrink-0 items-center space-x-1 px-3">
                      <CopyFeedback content={api.token} />
                      {canManage && (
                        <ActionButton
                          onClick={() => {
                            setDelKeyId(api.id)
                            setShowConfirmDelete(true)
                          }}
                        >
                          <span className="i-ri-delete-bin-line size-4" />
                        </ActionButton>
                      )}
                      {/* 二开部分 - 密钥额度限制编辑 */}
                      {isCurrentWorkspaceManager && (
                        <ActionButton
                          onClick={() => {
                            openSecretKeyQuotaEditModalExtend(api).then()
                          }}
                        >
                          <span className={`size-4 ${s.editIcon}`} />
                        </ActionButton>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="flex">
            {/* 二开部分 - 密钥额度限制：点击先弹出额度设置弹窗 */}
            <Button
              className={`mt-4 flex shrink-0 ${s.autoWidth}`}
              onClick={openSecretKeyQuotaSetModalExtend}
              disabled={!currentWorkspace.id || !canManage}
            >
              <span className="mr-1 i-heroicons-plus-20-solid flex size-4 shrink-0" />
              <div className="text-xs font-medium text-text-secondary">
                {t(($) => $['apiKeyModal.createNewSecretKey'], { ns: 'appApi' })}
              </div>
            </Button>
          </div>
          <AlertDialog open={showConfirmDelete} onOpenChange={handleDeleteConfirmOpenChange}>
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
                <AlertDialogCancelButton>
                  {t(($) => $['operation.cancel'], { ns: 'common' })}
                </AlertDialogCancelButton>
                <AlertDialogConfirmButton onClick={onDel}>
                  {t(($) => $['operation.confirm'], { ns: 'common' })}
                </AlertDialogConfirmButton>
              </AlertDialogActions>
            </AlertDialogContent>
          </AlertDialog>
          {/* ----------------------二开部分Begin - 密钥额度限制---------------------- */}
          <SecretKeyQuotaSetExtendModal
            className="shrink-0"
            isShow={isVisibleExtend}
            onClose={() => setVisibleExtend(false)}
            newKey={keyItem}
            onChange={handleSetKeyDataSetQuotas}
            onCreate={keyItem.id === '' ? onCreate : onEdit}
          />
          {/* ----------------------二开部分End - 密钥额度限制---------------------- */}
        </DialogContent>
      </Dialog>
      {isShow && (
        <SecretKeyGenerateModal
          className="shrink-0"
          isShow={isVisible}
          onClose={() => setIsVisible(false)}
          newKey={newKey}
        />
      )}
    </>
  )
}

export default SecretKeyModal
