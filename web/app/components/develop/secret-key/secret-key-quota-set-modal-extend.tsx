'use client'
import type { ApikeyItemResponseWithQuotaLimitExtend } from '@/models/app'
import { XMarkIcon } from '@heroicons/react/20/solid'
import { useTranslation } from 'react-i18next'
import Button from '@/app/components/base/button'
import Modal from '@/app/components/base/modal'
import DayLimitItemExtend from '@/app/components/base/param-item/day-limit-item-extend'
import MonthLimitItemExtend from '@/app/components/base/param-item/month-limit-item-extend'
import s from './style.module.css'

type ISecretKeyGenerateModalProps = {
  isShow: boolean
  onClose: () => void
  onCreate: () => void
  onChange: (keyItem: ApikeyItemResponseWithQuotaLimitExtend) => void
  newKey: ApikeyItemResponseWithQuotaLimitExtend
  className?: string
}

const SecretKeyQuotaSetExtendModal = ({
  isShow = false,
  onClose,
  onCreate,
  onChange,
  newKey,
  className,
}: ISecretKeyGenerateModalProps) => {
  const { t } = useTranslation()

  const handleParamChange = (key: string, value: any) => {
    if (key === 'day_limit_quota') {
      onChange({
        ...newKey,
        day_limit_quota: value,
      })
    }
    else if (key === 'month_limit_quota') {
      onChange({
        ...newKey,
        month_limit_quota: value,
      })
    }
    else if (key === 'description') {
      onChange({
        ...newKey,
        description: value,
      })
    }
  }

  const handleParamChangeDesc = (value: string) => {
    handleParamChange('description', value)
  }

  return (
    <Modal
      isShow={isShow}
      onClose={onClose}
      title={`${newKey?.id ? '编辑' : '创建'}${t('apiKeyModal.apiSecretKey', { ns: 'appApi' })}`}
      className={`px-8 ${className}`}
    >
      <XMarkIcon className={`absolute h-6 w-6 cursor-pointer text-gray-500 ${s.close}`} onClick={onClose} />
      <p className="mt-1 text-[13px] font-normal leading-5 text-gray-500">
        {t('apiKeyModal.apiSecretKeyTips', { ns: 'extend' })}
      </p>
      <div className="my-4">
        <input
          value={newKey?.description ?? ''}
          onChange={e => handleParamChangeDesc(e.target.value)}
          placeholder={t('apiKeyModal.descriptionPlaceholder', { ns: 'extend' }) || '密钥用途'}
          className="h-10 grow appearance-none rounded-lg border border-transparent bg-gray-100 px-3 text-sm font-normal caret-primary-600 outline-none placeholder:text-gray-400 hover:border hover:border-gray-300 hover:bg-gray-50 focus:border focus:border-gray-300 focus:bg-gray-50 focus:shadow-xs"
        />
      </div>
      <div className="my-4">
        <DayLimitItemExtend
          value={newKey?.day_limit_quota ?? -1}
          onChange={handleParamChange}
          enable={true}
        />
      </div>
      <div className="my-4">
        <MonthLimitItemExtend
          value={newKey?.month_limit_quota ?? -1}
          onChange={handleParamChange}
          enable={true}
        />
      </div>
      <div className="my-4 flex justify-end">
        <Button variant="primary" className={`shrink-0 ${s.w64}`} onClick={onCreate}>
          {newKey?.id ? t('operation.save', { ns: 'common' }) : t('operation.create', { ns: 'common' })}
        </Button>
      </div>

    </Modal>
  )
}

export default SecretKeyQuotaSetExtendModal
