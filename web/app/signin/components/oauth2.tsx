import React from 'react'
import { useRouter } from 'next/navigation'
import { useTranslation } from 'react-i18next'
import style from '../page.module.css'
import Button from '@/app/components/base/button'
import { cn } from '@/utils/classnames'
import { API_PREFIX } from '@/config'

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

  return <>
    <div className="mb-2">
      <a onClick={OAuth2Login}>
        <Button
          className="w-full"
        >
          <span className={
            cn(
              style.oauth2Icon,
              'mr-2 h-5 w-5',
            )
          }
          />
          <span className="truncate">{props.title === '' ? t('withSSO', { ns: 'login' }) : props.title}</span>
        </Button>
      </a>
    </div>
  </>
}
