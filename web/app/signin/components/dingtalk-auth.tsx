import React from 'react'
import { useRouter } from 'next/navigation'
import { useTranslation } from 'react-i18next'
import style from '../page.module.css'
import Button from '@/app/components/base/button'
import { cn } from '@/utils/classnames'
import { API_PREFIX } from '@/config'

type SocialAuthProps = {
  clientId: string
}

export default function DingTalkAuth(props: SocialAuthProps) {
  const { t } = useTranslation()
  const router = useRouter()

  /* Extend: start 钉钉快捷登录按钮 */
  const DingTalkCasLogin = () => {
    const params = new URLSearchParams()
    params.append('scope', 'openid')
    params.append('prompt', 'consent')
    params.append('response_type', 'code')
    params.append('client_id', props.clientId)
    params.append('redirect_uri', `${API_PREFIX}/ding-talk/third-party/login`)
    router.replace(`https://login.dingtalk.com/oauth2/auth?${params.toString()}`)
  }

  return <>
    <div className="mb-2">
      <a onClick={DingTalkCasLogin}>
        <Button
          className="w-full"
        >
          <span className={
            cn(
              style.dingIcon,
              'mr-2 h-5 w-5',
            )
          }
          />
          <span className="truncate">{t('sidebar.withDingTalk', { ns: 'extend' })}</span>
        </Button>
      </a>
    </div>
  </>
}
