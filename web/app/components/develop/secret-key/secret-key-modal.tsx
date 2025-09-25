'use client'
import {
  useState,
} from 'react'
import { useTranslation } from 'react-i18next'
import { RiDeleteBinLine } from '@remixicon/react'
import { PlusIcon, XMarkIcon } from '@heroicons/react/20/solid'
import useSWR, { useSWRConfig } from 'swr'
import SecretKeyGenerateModal from './secret-key-generate'
import SecretKeyQuotaSetExtendModal from './secret-key-quota-set-modal-extend' // 二开部分 - 密钥额度
import s from './style.module.css'
import ActionButton from '@/app/components/base/action-button'
import Modal from '@/app/components/base/modal'
import Button from '@/app/components/base/button'
import CopyFeedback from '@/app/components/base/copy-feedback'
import {
  createApikey as createAppApikey,
  delApikey as delAppApikey,
  editApikey,
  fetchApiKeysList as fetchAppApiKeysList,
} from '@/service/apps'
import {
  createApikey as createDatasetApikey,
  delApikey as delDatasetApikey,
  fetchApiKeysList as fetchDatasetApiKeysList,
} from '@/service/datasets'
import type { ApiKeyItemResponse, ApikeyItemResponseWithQuotaLimitExtend, CreateApiKeyResponse } from '@/models/app' // 二开部分End - 密钥额度
import Loading from '@/app/components/base/loading'
import Confirm from '@/app/components/base/confirm'
import useTimestamp from '@/hooks/use-timestamp'
import { useAppContext } from '@/context/app-context'

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
  const { mutate } = useSWRConfig()
  const commonParams = appId
    ? { url: `/apps/${appId}/api-keys`, params: {} }
    : { url: '/datasets/api-keys', params: {} }
  const fetchApiKeysList = appId ? fetchAppApiKeysList : fetchDatasetApiKeysList
  const { data: apiKeysList } = useSWR(commonParams, fetchApiKeysList)

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
    mutate(commonParams)
  }

  const onCreate = async () => {
    // 二开部分 - 密钥额度，新增body传值
    const params = appId
      ? {
        url: `/apps/${appId}/api-keys`,
        body: {
          description: keyItem.description,
          day_limit_quota: keyItem.day_limit_quota,
          month_limit_quota: keyItem.month_limit_quota,
        },
      }
      : { url: '/datasets/api-keys', body: {} }
    const createApikey = appId ? createAppApikey : createDatasetApikey
    const res = await createApikey(params)
    setVisible(true)
    setVisibleExtend(false) // 二开部分 - 密钥额度，关闭创建额度编辑框
    setNewKey(res)
    mutate(commonParams)
  }

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
    mutate(commonParams)
  }
  // 二开部分 Begin - 密钥额度限制编辑

  const generateToken = (token: string) => {
    return `${token.slice(0, 3)}...${token.slice(-20)}`
  }

  return (
    <Modal isShow={isShow} onClose={onClose} title={`${t('appApi.apiKeyModal.apiSecretKey')}`} className={`${s.customModalExtend} flex flex-col px-8`}> {/* 二开部分 - 密钥额度限制，由customModal 改为 customModalExtend */}
      <div className="-mr-2 -mt-6 mb-4 flex justify-end">
        <XMarkIcon className="h-6 w-6 cursor-pointer text-text-tertiary" onClick={onClose} />
      </div>
      <p className='mt-1 shrink-0 text-[13px] font-normal leading-5 text-text-tertiary'>{t('appApi.apiKeyModal.apiSecretKeyTips')}</p>
      {!apiKeysList && <div className='mt-4'><Loading /></div>}
      {
        !!apiKeysList?.data?.length && (
          <div className='mt-4 flex grow flex-col overflow-hidden'>
            <div className='flex h-9 shrink-0 items-center border-b border-divider-regular text-xs font-semibold text-text-tertiary'>
              <div className='w-64 shrink-0 px-3'>{t('appApi.apiKeyModal.secretKey')}</div>
              <div className='w-[150px] shrink-0 px-3'>{t('appApi.apiKeyModal.created')}</div>{/* 二开部分 - 密钥额度限制，调整宽度 200 改为 150 ---------------------- */}
              <div className='w-[150px] shrink-0 px-3'>{t('appApi.apiKeyModal.lastUsed')}</div>{/* 二开部分 - 密钥额度限制，调整宽度 200 改为 150 ---------------------- */}
              {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
              <div className='w-[100px] shrink-0 px-3'>{t('extend.apiKeyModal.descriptionPlaceholder')}</div>
              <div className='w-[200px] shrink-0 px-3'>{t('extend.apiKeyModal.dayLimit')}</div>
              <div className='w-[200px] shrink-0 px-3'>{t('extend.apiKeyModal.monthLimit')}</div>
              <div className='w-[200px] shrink-0 px-3'>{t('extend.apiKeyModal.accumulatedLimit')}</div>
              {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
              <div className='grow px-3'></div>
            </div>
            <div className='grow overflow-auto'>
              {apiKeysList.data.map(api => (
                <div className='flex h-9 items-center border-b border-divider-regular text-sm font-normal text-text-secondary' key={api.id}>
                  <div className='w-64 shrink-0 truncate px-3 font-mono'>{generateToken(api.token)}</div>
                  <div className='w-[150px] shrink-0 truncate px-3'>{formatTime(Number(api.created_at), t('appLog.dateTimeFormat') as string)}</div>{/* 二开部分 - 密钥额度限制，调整宽度 200 改为 150 ---------------------- */}
                  <div className='w-[150px] shrink-0 truncate px-3'>{api.last_used_at ? formatTime(Number(api.last_used_at), t('appLog.dateTimeFormat') as string) : t('appApi.never')}</div>{/* 二开部分 - 密钥额度限制，调整宽度 200 改为 150 ---------------------- */}
                  {/* ---------------------- 二开部分Begin - 密钥额度限制 ---------------------- */}
                  <div className='w-[100px] shrink-0 truncate px-3'>{api.description}</div>
                  <div className='w-[200px] shrink-0 truncate px-3'>$ {api.day_used_quota} / {api.day_limit_quota === -1 ? t('extend.apiKeyModal.noLimit') : `$ ${api.day_limit_quota}`}</div>
                  <div className='w-[200px] shrink-0 truncate px-3'>$ {api.month_used_quota} / {api.month_limit_quota === -1 ? t('extend.apiKeyModal.noLimit') : `$ ${api.month_limit_quota}`}</div>
                  <div className='w-[200px] shrink-0 truncate px-3'>$ {api.accumulated_quota}</div>
                  {/* ---------------------- 二开部分End - 密钥额度限制 ---------------------- */}
                  <div className='flex grow space-x-2 px-3'>
                    <CopyFeedback content={api.token} />
                    {isCurrentWorkspaceManager && (
                      <ActionButton
                        onClick={() => {
                          setDelKeyId(api.id)
                          setShowConfirmDelete(true)
                        }}
                      >
                        <RiDeleteBinLine className='h-4 w-4' />
                      </ActionButton>
                    )}
                    {/* // 二开部分 End - 密钥额度限制编辑 */}
                    {isCurrentWorkspaceManager
                      && <div className={`flex items-center justify-center flex-shrink-0 w-6 h-6 rounded-lg cursor-pointer ${s.editIcon}`} onClick={() => {
                        openSecretKeyQuotaEditModalExtend(api)
                      }}>
                      </div>
                    }
                    {/* // 二开部分 Begin - 密钥额度限制编辑 */}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )
      }
      <div className='flex'>
        <Button className={`mt-4 flex shrink-0 ${s.autoWidth}`} onClick={openSecretKeyQuotaSetModalExtend} disabled={ !currentWorkspace || !isCurrentWorkspaceManager}> {/* 二开部分Begin - 密钥额度限制由onClick改为openSecretKeyQuotaSetModalExtend */}
          <PlusIcon className='mr-1 flex h-4 w-4 shrink-0' />
          <div className='text-xs font-medium text-text-secondary'>{t('appApi.apiKeyModal.createNewSecretKey')}</div>
        </Button>
      </div>
      <SecretKeyGenerateModal className='shrink-0' isShow={isVisible} onClose={() => setVisible(false)} newKey={newKey} />
      {showConfirmDelete && (
        <Confirm
          title={`${t('appApi.actionMsg.deleteConfirmTitle')}`}
          content={`${t('appApi.actionMsg.deleteConfirmTips')}`}
          isShow={showConfirmDelete}
          onConfirm={onDel}
          onCancel={() => {
            setDelKeyId('')
            setShowConfirmDelete(false)
          }}
        />
      )}

      {/* ----------------------二开部分Begin - 密钥额度限制---------------------- */}
      <SecretKeyQuotaSetExtendModal className='flex-shrink-0' isShow={isVisibleExtend} onClose={() => setVisibleExtend(false)} newKey={keyItem} onChange={handleSetKeyDataSetQuotas} onCreate={keyItem.id==''? onCreate:onEdit}/>
      {/* ----------------------二开部分End - 密钥额度限制---------------------- */}
    </Modal >
  )
}

export default SecretKeyModal
