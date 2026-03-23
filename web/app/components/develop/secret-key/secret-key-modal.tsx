'use client'
import type { ApiKeyItemResponse, ApikeyItemResponseWithQuotaLimitExtend, CreateApiKeyResponse } from '@/models/app'
import { PlusIcon, XMarkIcon } from '@heroicons/react/20/solid'
import { RiDeleteBinLine } from '@remixicon/react'
import {
  useState,
} from 'react'
import { useTranslation } from 'react-i18next'
import ActionButton from '@/app/components/base/action-button'
import Button from '@/app/components/base/button'
import Confirm from '@/app/components/base/confirm'
import CopyFeedback from '@/app/components/base/copy-feedback'
import Loading from '@/app/components/base/loading'
import Modal from '@/app/components/base/modal'
import { useAppContext } from '@/context/app-context'
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
// 二开部分End - 密钥额度
import { useDatasetApiKeys, useInvalidateDatasetApiKeys } from '@/service/knowledge/use-dataset'
import { useAppApiKeys, useInvalidateAppApiKeys } from '@/service/use-apps'
import SecretKeyGenerateModal from './secret-key-generate'
// 二开部分Start - 密钥额度
import SecretKeyQuotaSetExtendModal from './secret-key-quota-set-modal-extend'
import s from './style.module.css'

type ISecretKeyModalProps = {
  isShow: boolean
  appId?: string
  onClose: () => void
}

const SecretKeyModal = ({
  isShow = false,
  appId,
  onClose,
}: ISecretKeyModalProps) => {
  const { t } = useTranslation()
  const { formatTime } = useTimestamp()
  const { currentWorkspace, isCurrentWorkspaceManager, isCurrentWorkspaceEditor } = useAppContext()
  const [showConfirmDelete, setShowConfirmDelete] = useState(false)
  const [isVisible, setVisible] = useState(false)
  const [newKey, setNewKey] = useState<CreateApiKeyResponse | undefined>(undefined)
  const invalidateAppApiKeys = useInvalidateAppApiKeys()
  const invalidateDatasetApiKeys = useInvalidateDatasetApiKeys()
  const { data: appApiKeys, isLoading: isAppApiKeysLoading } = useAppApiKeys(appId, { enabled: !!appId && isShow })
  const { data: datasetApiKeys, isLoading: isDatasetApiKeysLoading } = useDatasetApiKeys({ enabled: !appId && isShow })
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
  // ---------------------- 二开部分End - 密钥额度 ----------------------

  // 二开部分 Begin - 密钥额度限制编辑
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
  }
  // 二开部分 Begin - 密钥额度限制编辑

  const [delKeyID, setDelKeyId] = useState('')

  const onDel = async () => {
    setShowConfirmDelete(false)
    if (!delKeyID)
      return

    const delApikey = appId ? delAppApikey : delDatasetApikey
    const params = appId
      ? { url: `/apps/${appId}/api-keys/${delKeyID}`, params: {} }
      : { url: `/datasets/api-keys/${delKeyID}`, params: {} }
    await delApikey(params)
    if (appId)
      invalidateAppApiKeys(appId)
    else
      invalidateDatasetApiKeys()
  }

  const onCreate = async () => {
    const params = appId
      ? { url: `/apps/${appId}/api-keys`, body: {} }
      : { url: '/datasets/api-keys', body: {} }
    const createApikey = appId ? createAppApikey : createDatasetApikey
    const res = await createApikey(params)
    setVisible(true)
    setNewKey(res)
    if (appId)
      invalidateAppApiKeys(appId)
    else
      invalidateDatasetApiKeys()
  }

  const generateToken = (token: string) => {
    return `${token.slice(0, 3)}...${token.slice(-20)}`
  }

  return (
    <Modal isShow={isShow} onClose={onClose} title={`${t('apiKeyModal.apiSecretKey', { ns: 'appApi' })}`} className={`${s.customModal} flex flex-col px-8`}>
      <div className="-mr-2 -mt-6 mb-4 flex justify-end">
        <XMarkIcon className="h-6 w-6 cursor-pointer text-text-tertiary" onClick={onClose} />
      </div>
      <p className="mt-1 shrink-0 text-[13px] font-normal leading-5 text-text-tertiary">{t('apiKeyModal.apiSecretKeyTips', { ns: 'appApi' })}</p>
      {isApiKeysLoading && <div className="mt-4"><Loading /></div>}
      {
        !!apiKeysList?.data?.length && (
          <div className="mt-4 flex grow flex-col overflow-hidden">
            <div className="flex h-9 shrink-0 items-center border-b border-divider-regular text-xs font-semibold text-text-tertiary">
              <div className="w-64 shrink-0 px-3">{t('apiKeyModal.secretKey', { ns: 'appApi' })}</div>
              <div className="w-[200px] shrink-0 px-3">{t('apiKeyModal.created', { ns: 'appApi' })}</div>
              <div className="w-[200px] shrink-0 px-3">{t('apiKeyModal.lastUsed', { ns: 'appApi' })}</div>
              {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
              <div className="w-[100px] shrink-0 px-3">{t('apiKeyModal.descriptionPlaceholder', { ns: 'extend' })}</div>
              <div className="w-[200px] shrink-0 px-3">{t('apiKeyModal.dayLimit', { ns: 'extend' })}</div>
              <div className="w-[200px] shrink-0 px-3">{t('apiKeyModal.monthLimit', { ns: 'extend' })}</div>
              <div className="w-[200px] shrink-0 px-3">{t('apiKeyModal.accumulatedLimit', { ns: 'extend' })}</div>
              {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
              <div className="grow px-3"></div>
            </div>
            <div className="grow overflow-auto">
              {apiKeysList.data.map(api => (
                <div className="flex h-9 items-center border-b border-divider-regular text-sm font-normal text-text-secondary" key={api.id}>
                  <div className="w-64 shrink-0 truncate px-3 font-mono">{generateToken(api.token)}</div>
                  <div className="w-[200px] shrink-0 truncate px-3">{formatTime(Number(api.created_at), t('dateTimeFormat', { ns: 'appLog' }) as string)}</div>
                  <div className="w-[200px] shrink-0 truncate px-3">{api.last_used_at ? formatTime(Number(api.last_used_at), t('dateTimeFormat', { ns: 'appLog' }) as string) : t('never', { ns: 'appApi' })}</div>
                  {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
                  <div className="w-[100px] shrink-0 truncate px-3">{api.description}</div>
                  <div className="w-[200px] shrink-0 truncate px-3">
                    $
                    {api.day_used_quota}
                    {' '}
                    /
                    {api.day_limit_quota === -1 ? t('apiKeyModal.noLimit', { ns: 'extend' }) : `$ ${api.day_limit_quota}`}
                  </div>
                  <div className="w-[200px] shrink-0 truncate px-3">
                    $
                    {api.month_used_quota}
                    {' '}
                    /
                    {api.month_limit_quota === -1 ? t('apiKeyModal.noLimit', { ns: 'extend' }) : `$ ${api.month_limit_quota}`}
                  </div>
                  <div className="w-[200px] shrink-0 truncate px-3">
                    $
                    {api.accumulated_quota}
                  </div>
                  {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
                  <div className="flex grow space-x-2 px-3">
                    <CopyFeedback content={api.token} />
                    {isCurrentWorkspaceManager && (
                      <ActionButton
                        onClick={() => {
                          setDelKeyId(api.id)
                          setShowConfirmDelete(true)
                        }}
                      >
                        <RiDeleteBinLine className="h-4 w-4" />
                      </ActionButton>
                    )}
                    {/* // 二开部分 End - 密钥额度限制编辑 */}
                    {isCurrentWorkspaceManager && (
                      <div
                        className={`flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-lg ${s.editIcon}`}
                        onClick={() => {
                          openSecretKeyQuotaEditModalExtend(api).then()
                        }}
                      >
                      </div>
                    )}
                    {/* // 二开部分 Begin - 密钥额度限制编辑 */}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )
      }
      <div className="flex">
        <Button className={`mt-4 flex shrink-0 ${s.autoWidth}`} onClick={onCreate} disabled={!currentWorkspace || !isCurrentWorkspaceEditor}>
          <PlusIcon className="mr-1 flex h-4 w-4 shrink-0" />
          <div className="text-xs font-medium text-text-secondary">{t('apiKeyModal.createNewSecretKey', { ns: 'appApi' })}</div>
        </Button>
      </div>
      <SecretKeyGenerateModal className="shrink-0" isShow={isVisible} onClose={() => setVisible(false)} newKey={newKey} />
      {showConfirmDelete && (
        <Confirm
          title={`${t('actionMsg.deleteConfirmTitle', { ns: 'appApi' })}`}
          content={`${t('actionMsg.deleteConfirmTips', { ns: 'appApi' })}`}
          isShow={showConfirmDelete}
          onConfirm={onDel}
          onCancel={() => {
            setDelKeyId('')
            setShowConfirmDelete(false)
          }}
        />
      )}

      {/* ----------------------二开部分Begin - 密钥额度限制---------------------- */}
      <SecretKeyQuotaSetExtendModal className="shrink-0" isShow={isVisibleExtend} onClose={() => setVisibleExtend(false)} newKey={keyItem} onChange={handleSetKeyDataSetQuotas} onCreate={keyItem.id === '' ? onCreate : onEdit} />
      {/* ----------------------二开部分End - 密钥额度限制---------------------- */}
    </Modal>
  )
}

export default SecretKeyModal
