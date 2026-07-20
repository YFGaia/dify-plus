'use client'
import type { ApikeyItemResponseWithQuotaLimitExtend } from '@/models/app'
import { XMarkIcon } from '@heroicons/react/20/solid'
import { Button } from '@langgenius/dify-ui/button'
import { Dialog, DialogContent, DialogTitle } from '@langgenius/dify-ui/dialog'
import { useTranslation } from 'react-i18next'
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

  const handleParamChange = (key: string, value: number) => {
    if (key === 'day_limit_quota') {
      onChange({
        ...newKey,
        day_limit_quota: value,
      })
    } else if (key === 'month_limit_quota') {
      onChange({
        ...newKey,
        month_limit_quota: value,
      })
    }
  }

  const handleParamChangeDesc = (value: string) => {
    onChange({
      ...newKey,
      description: value,
    })
  }

  return (
    <Dialog
      open={isShow}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className={`px-8 ${className}`}>
        <DialogTitle className="title-2xl-semi-bold text-text-primary">
          {`${newKey?.id ? '编辑' : '创建'}${t(($) => $['apiKeyModal.apiSecretKey'], { ns: 'appApi' })}`}
        </DialogTitle>
        <XMarkIcon
          className={`absolute h-6 w-6 cursor-pointer text-gray-500 ${s.close}`}
          onClick={onClose}
        />
        <p className="mt-1 text-[13px] leading-5 font-normal text-gray-500">
          {t(($) => $['apiKeyModal.apiSecretKeyTips'], { ns: 'extend' })}
        </p>
        <div className="my-4">
          <input
            value={newKey?.description ?? ''}
            onChange={(e) => handleParamChangeDesc(e.target.value)}
            placeholder={
              t(($) => $['apiKeyModal.descriptionPlaceholder'], { ns: 'extend' }) || '密钥用途'
            }
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
            {newKey?.id
              ? t(($) => $['operation.save'], { ns: 'common' })
              : t(($) => $['operation.create'], { ns: 'common' })}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default SecretKeyQuotaSetExtendModal
