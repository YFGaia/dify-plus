'use client'

// 二开部分：额度徽章。原实现位于旧 web/app/components/header/account-money-extend/
// （上游 1.15.0 删除旧 header 后重做到 main-nav 体系）。
// 汇率不再硬编码（原 6.97），改读后端 login_config 下发的 rmb_to_usd_rate（配置 RMB_TO_USD_RATE）。
import { cn } from '@langgenius/dify-ui/cn'
import { useQuery, useSuspenseQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { systemFeaturesQueryOptions } from '@/features/system-features/client'
import { asSystemFeaturesExtend } from '@/features/system-features/extend'
import { fetchUserMoney } from '@/service/common-extend'

const AccountMoneyExtend = () => {
  const { t } = useTranslation()
  const { data: systemFeatures } = useSuspenseQuery(systemFeaturesQueryOptions())
  const exchangeRate = asSystemFeaturesExtend(systemFeatures).rmb_to_usd_rate
  const { data: userMoney } = useQuery({
    queryKey: ['common-extend', 'account-money'],
    queryFn: fetchUserMoney,
  })

  if (!userMoney) return null

  // 计算额度（确保使用数字类型）
  const usedQuota = Number(userMoney.used_quota) || 0
  const totalQuota = Number(userMoney.total_quota) || 0
  const remainingQuota = totalQuota - usedQuota

  // 当总额度为0时不显示
  if (totalQuota === 0) return null

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
    <div className="mt-2 flex items-center overflow-hidden rounded-md border border-divider-regular text-xs leading-[18px]">
      <div className="flex items-center bg-background-default-dimmed px-2 py-1 font-medium text-text-secondary">
        {t(($) => $['user.credit'], { ns: 'extend' })}
      </div>
      <div className="flex min-w-0 flex-1 items-center border-l border-divider-regular bg-background-default px-2 py-1.5">
        <span className="mr-1 text-text-tertiary">
          {t(($) => $['user.used'], { ns: 'extend' })}
        </span>
        <span className={cn('font-bold transition-all duration-300', alertColorClass)}>
          ¥{usedRMB}
        </span>
        <span className="mx-1 text-text-quaternary">/</span>
        <span className="truncate text-text-tertiary">
          ¥{totalRMB.replace(/\B(?=(?:\d{3})+(?!\d))/g, ',')}
        </span>
      </div>
    </div>
  )
}

export default AccountMoneyExtend
