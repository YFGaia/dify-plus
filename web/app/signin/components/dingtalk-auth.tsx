import type { UseQueryResult } from '@tanstack/react-query'
import type { consoleClient } from '@/service/console'
import { Button, buttonVariants } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { useTranslation } from 'react-i18next'
import { API_PREFIX } from '@/config'
import { dingtalkErrorMessageKey } from '@/features/dingtalk/error-message-key'
import { useSearchParams } from '@/next/navigation'
import style from '../page.module.css'

type Display = Awaited<ReturnType<typeof consoleClient.auth.dingtalk.display.get>>

export default function DingTalkAuth({ query }: { query: UseQueryResult<Display> }) {
  const { t } = useTranslation('extend')
  const params = useSearchParams()
  const error = params.get('dingtalk_error')
  if (query.isPending || query.isFetching)
    return (
      <p role="status" className="text-sm text-text-secondary">
        {t(($) => $['systemManage.common.loading'])}
      </p>
    )
  if (query.isError)
    return (
      <div className="space-y-2">
        <p role="alert" className="text-sm text-text-destructive">
          {t(($) => $['systemManage.dingtalk.loginFailed'])}
        </p>
        <Button
          onClick={() => {
            void query.refetch()
          }}
        >
          {t(($) => $['operation.retry'], { ns: 'common' })}
        </Button>
      </div>
    )
  const showEntry =
    query.isSuccess &&
    query.data.enabled &&
    query.data.start_path === '/console/api/auth/dingtalk/start'
  return (
    <div className="mb-2 space-y-2">
      {error && (
        <p role="alert" className="text-sm text-text-destructive">
          {t(($) => $[dingtalkErrorMessageKey(error)])}
        </p>
      )}
      {showEntry && (
        <a
          className={cn(buttonVariants(), 'w-full')}
          href={`${API_PREFIX.replace(/\/+$/, '')}${query.data.start_path?.slice('/console/api'.length)}`}
        >
          <span aria-hidden="true" className={cn(style.dingIcon, 'size-5')} />
          <span>{t(($) => $['sidebar.withDingTalk'])}</span>
        </a>
      )}
    </div>
  )
}
