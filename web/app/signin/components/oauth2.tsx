import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import React from 'react'
import { useTranslation } from 'react-i18next'
import { API_PREFIX } from '@/config'
import { useRouter } from '@/next/navigation'
import style from '../page.module.css'

type OAuth2Props = {
  title: string
}

export default function OAuth2(props: OAuth2Props) {
  const { t } = useTranslation()
  const router = useRouter()

  /* Extend: start 钉钉快捷登录按钮 */
  const OAuth2Login = () => {
    router.replace(`${API_PREFIX}/oauth/login/oauth2`)
  }

  return (
    <>
      <div className="mb-2">
        <Button className="w-full" onClick={OAuth2Login}>
          <span className={cn(style.oauth2Icon, 'mr-2 h-5 w-5')} />
          <span className="truncate">
            {props.title === '' ? t(($) => $.withSSO, { ns: 'login' }) : props.title}
          </span>
        </Button>
      </div>
    </>
  )
}
