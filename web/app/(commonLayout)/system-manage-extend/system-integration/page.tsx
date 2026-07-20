'use client'

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import DingTalkConfig from './dingtalk-config'
import EmailApiConfig from './email-api-config'
import ForwardTokenList from './forward-token-list'
import OAuth2Config from './oauth2-config'

type Tab = 'dingtalk' | 'oauth2' | 'email-api' | 'forward-token'

const SystemIntegrationPage = () => {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<Tab>('dingtalk')

  const tabs: { key: Tab, label: string }[] = [
    { key: 'dingtalk', label: t(($) => $['systemManage.dingtalk.title'], { ns: 'extend' }) },
    { key: 'oauth2', label: t(($) => $['systemManage.oauth2.title'], { ns: 'extend' }) },
    { key: 'email-api', label: t(($) => $['systemManage.emailApi.title'], { ns: 'extend' }) },
    { key: 'forward-token', label: t(($) => $['systemManage.forwardToken.title'], { ns: 'extend' }) },
  ]

  return (
    <div>
      <h1 className="mb-6 text-xl font-semibold text-text-primary">
        {t(($) => $['systemManage.integration.title'], { ns: 'extend' })}
      </h1>

      {/* Tab 切换 */}
      <div className="mb-6 flex border-b border-divider-subtle">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`mr-4 border-b-2 px-1 pb-3 text-sm font-medium transition-colors ${
              activeTab === tab.key
                ? 'border-text-accent text-text-accent'
                : 'border-transparent text-text-tertiary hover:text-text-secondary'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      <div>
        {activeTab === 'dingtalk' && <DingTalkConfig />}
        {activeTab === 'oauth2' && <OAuth2Config />}
        {activeTab === 'email-api' && <EmailApiConfig />}
        {activeTab === 'forward-token' && <ForwardTokenList />}
      </div>
    </div>
  )
}

export default SystemIntegrationPage
