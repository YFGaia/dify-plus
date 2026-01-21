'use client'
import React, { useEffect, useState } from 'react'
import { fetchUserMoney } from '@/service/common-extend'
import type { UserMoney } from '@/models/common-extend'
import { cn } from '@/utils/classnames'
import {useTranslation} from "react-i18next";

const AccountMoneyExtend = () => {
  const [userMoney, setUserMoney] = useState<UserMoney>({ used_quota: 0, total_quota: 0 })
  const [isFetched, setIsFetched] = useState(false)
  const exchangeRate = 6.97 // 美元转人民币固定汇率
  const { t } = useTranslation()

  const getUserMoney = async () => {
    const data: any = await fetchUserMoney()
    setUserMoney(data)
  }

  useEffect(() => {
    getUserMoney()
    setIsFetched(true)
  }, [])

  if (!isFetched)
    return null

  // 计算额度（确保使用数字类型）
  const usedQuota = Number(userMoney.used_quota) || 0
  const totalQuota = Number(userMoney.total_quota) || 0
  const remainingQuota = totalQuota - usedQuota

  // 当总额度为0时不显示
  if (totalQuota === 0)
    return null

  // 转换为人民币并保留2位小数
  const usedRMB = (usedQuota * exchangeRate).toFixed(2)
  const totalRMB = (totalQuota * exchangeRate).toFixed(2)
  const remainingRMB = (remainingQuota * exchangeRate).toFixed(2)

  // 判断警示级别
  const isRedAlert = Number(remainingRMB) < 10 // 余额不足10元人民币，显示红色
  const isYellowAlert = Number(usedRMB) > 50 && !isRedAlert // 使用超过50元人民币，显示黄色

  // 根据警示级别设置颜色
  const alertColorClass = isRedAlert
    ? 'text-text-destructive'
    : isYellowAlert
      ? 'text-text-warning'
      : 'text-text-secondary'

  return (
    <div
      rel='noopener noreferrer'
      className='flex items-center overflow-hidden rounded-md border border-divider-regular text-xs leading-[18px]'
    >
      <div className='flex items-center bg-background-default-dimmed px-2 py-1 font-medium text-text-secondary'>
        {t('user.credit', { ns: 'extend' })}
      </div>
      <div className='flex items-center border-l border-divider-regular bg-background-default px-2 py-1.5'>
        <span className='mr-1 text-text-tertiary'>{t('user.used', { ns: 'extend' })}</span>
        <span
          className={cn(
            'font-bold transition-all duration-300',
            alertColorClass,
            'text-sm md:text-base', // 默认字体稍大，响应式设计
          )}
        >
          ¥{usedRMB}
        </span>
        <span className='mx-1 text-text-quaternary'>/</span>
        <span className='text-text-tertiary'>
          ¥{totalRMB.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
        </span>
      </div>
    </div>
  )
}

export default AccountMoneyExtend
