'use client'
import type { FC } from 'react'
import * as React from 'react'
import { useTranslation } from 'react-i18next'
import ParamItem from '.'

type Props = {
  className?: string
  value: number
  onChange: (key: string, value: number) => void
  enable: boolean
}

const VALUE_LIMIT = {
  default: -1,
  step: 10,
  min: -1,
  max: 9999,
}

const key = 'day_limit_quota'
const DayLimitItemExtend: FC<Props> = ({
  className,
  value,
  enable,
  onChange,
}) => {
  const { t } = useTranslation()
  const handleParamChange = (key: string, value: number) => {
    let notOutRangeValue = Number.parseFloat(value.toFixed(2))
    notOutRangeValue = Math.max(VALUE_LIMIT.min, notOutRangeValue)
    notOutRangeValue = Math.min(VALUE_LIMIT.max, notOutRangeValue)
    onChange(key, notOutRangeValue)
  }
  return (
    <ParamItem
      className={className}
      id={key}
      name={t('apiKeyModal.dayLimitItemName', { ns: 'extend' })}
      tip={t('apiKeyModal.noLimitTips', { ns: 'extend' }) as string}
      {...VALUE_LIMIT}
      value={value}
      enable={enable}
      onChange={handleParamChange}
    />
  )
}
export default React.memo(DayLimitItemExtend)
