'use client'

import { cn } from '@langgenius/dify-ui/cn'
import { atom, useAtomValue } from 'jotai'
import { atomWithQuery } from 'jotai-tanstack-query'
import { useTranslation } from 'react-i18next'
import { z } from 'zod'
import { currentWorkspaceAtom } from '@/context/workspace-state'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { loginConfigQueryOptions } from '@/features/system-features/client'
import { consoleQuery } from '@/service/console'

// The generated response is unknown. Validate at the query boundary so malformed
// balances never enter the cache as successful data.
const quota = z
  .union([
    z.number(),
    z.string().trim().regex(/^\d+(?:\.\d+)?$/).transform(Number),
  ])
  .pipe(z.number().finite().nonnegative())
const accountMoneySchema = z.object({ total_quota: quota, used_quota: quota })

const accountProfileQueryAtom = atomWithQuery(() => userProfileQueryOptions())
const balanceIdentityAtom = atom((get) => {
  const workspace = get(currentWorkspaceAtom)
  const profile = get(accountProfileQueryAtom)
  const accountId = profile.isSuccess ? profile.data.profile.id : undefined
  return accountId && workspace.id
    ? { accountId, workspaceId: workspace.id }
    : { accountId: null, workspaceId: null }
})
const balanceConfigQueryAtom = atomWithQuery((get) => {
  const identity = get(balanceIdentityAtom)
  return { ...loginConfigQueryOptions(identity), enabled: identity.accountId !== null }
})
const accountMoneyQueryAtom = atomWithQuery((get) => {
  const identity = get(balanceIdentityAtom)
  const options = consoleQuery.account.money.get.queryOptions({
    queryKey: [...consoleQuery.account.money.get.queryKey(), identity],
    enabled: identity.accountId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  })
  return {
    ...options,
    queryFn: async (context: Parameters<typeof options.queryFn>[0]) =>
      accountMoneySchema.parse(await options.queryFn(context)),
  }
})

const AccountMoneyExtend = () => {
  const { t } = useTranslation()
  const identity = useAtomValue(balanceIdentityAtom)
  const config = useAtomValue(balanceConfigQueryAtom)
  const money = useAtomValue(accountMoneyQueryAtom)
  const exchangeRate = config.data?.rmb_to_usd_rate
  if (
    identity.accountId === null
    || !config.isSuccess
    || !money.isSuccess
    || exchangeRate === undefined
    || !Number.isFinite(exchangeRate)
    || exchangeRate <= 0
  ) return null
  const userMoney = money.data

  const usedQuota = userMoney.used_quota
  const totalQuota = userMoney.total_quota
  const remainingQuota = totalQuota - usedQuota
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
    <div
      role="status"
      aria-label={t(($) => $['user.credit'], { ns: 'extend' })}
      className="mt-2 flex items-center overflow-hidden rounded-md border border-divider-regular text-xs leading-[18px]"
    >
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
