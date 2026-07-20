import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import React from 'react'
import { useTranslation } from 'react-i18next'
import { API_PREFIX } from '@/config'
import { useRouter } from '@/next/navigation'
import style from '../page.module.css'

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

  return (
    <>
      <div className="mb-2">
        <Button className="w-full" onClick={DingTalkCasLogin}>
          <span className={cn(style.dingIcon, 'mr-2 h-5 w-5')} />
          <span className="truncate">{t(($) => $['sidebar.withDingTalk'], { ns: 'extend' })}</span>
        </Button>
      </div>
    </>
  )
}
