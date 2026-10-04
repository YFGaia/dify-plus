'use client'

import { cn } from '@langgenius/dify-ui/cn'
import { useAtomValue } from 'jotai'
import { parseAsStringLiteral, useQueryState } from 'nuqs'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { isCurrentWorkspaceOwnerAtom } from '@/context/workspace-state'
import { CasdoorConfigurationForm } from '@/features/casdoor/configuration-form'
import { useCasdoorManagementAccess } from '@/features/casdoor/management-access/use-casdoor-management-access'
import DingTalkConfig from './dingtalk-config'
import EmailApiConfig from './email-api-config'
import ForwardTokenList from './forward-token-list'
import OAuth2Config from './oauth2-config'

type Tab = 'dingtalk' | 'oauth2' | 'email-api' | 'forward-token'
const casdoorTabParser = parseAsStringLiteral(['casdoor']).withOptions({ history: 'push' })

const SystemIntegrationPage = () => {
  const { t } = useTranslation()
  const isOwner = useAtomValue(isCurrentWorkspaceOwnerAtom)
  const { canManageCasdoor, isPending } = useCasdoorManagementAccess()
  const [legacyTab, setLegacyTab] = useState<Tab>('dingtalk')
  const [urlTab, setUrlTab] = useQueryState('tab', casdoorTabParser)
  const activeTab = !isOwner || urlTab === 'casdoor' ? 'casdoor' : legacyTab

  if (!isOwner && !canManageCasdoor) {
    return (
      <p role={isPending ? 'status' : 'alert'}>
        {t(
          ($) => $[isPending ? 'systemManage.casdoor.loading' : 'systemManage.common.noPermission'],
          { ns: 'extend' },
        )}
      </p>
    )
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'dingtalk', label: t(($) => $['systemManage.dingtalk.title'], { ns: 'extend' }) },
    { key: 'oauth2', label: t(($) => $['systemManage.oauth2.title'], { ns: 'extend' }) },
    { key: 'email-api', label: t(($) => $['systemManage.emailApi.title'], { ns: 'extend' }) },
    {
      key: 'forward-token',
      label: t(($) => $['systemManage.forwardToken.title'], { ns: 'extend' }),
    },
  ]

  return (
    <div>
      <h1 className="mb-6 text-xl font-semibold text-text-primary">
        {t(($) => $['systemManage.integration.title'], { ns: 'extend' })}
      </h1>

      {/* Tab 切换 */}
      <div className="mb-6 flex border-b border-divider-subtle">
        {isOwner &&
          tabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              aria-pressed={activeTab === tab.key}
              onClick={() => {
                setLegacyTab(tab.key)
                if (urlTab === 'casdoor') void setUrlTab(null)
              }}
              className={`mr-4 border-b-2 px-1 pb-3 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-state-accent-solid ${
                activeTab === tab.key
                  ? 'border-text-accent text-text-accent'
                  : 'border-transparent text-text-tertiary hover:text-text-secondary'
              }`}
            >
              {tab.label}
            </button>
          ))}
        <button
          type="button"
          aria-pressed={activeTab === 'casdoor'}
          onClick={() => {
            void setUrlTab('casdoor')
          }}
          className={cn(
            'mr-4 border-b-2 px-1 pb-3 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-state-accent-solid',
            activeTab === 'casdoor'
              ? 'border-text-accent text-text-accent'
              : 'border-transparent text-text-tertiary hover:text-text-secondary',
          )}
        >
          {t(($) => $['systemManage.casdoor.title'], { ns: 'extend' })}
        </button>
      </div>

      {/* Tab 内容 */}
      <div>
        {activeTab === 'dingtalk' && <DingTalkConfig />}
        {activeTab === 'oauth2' && <OAuth2Config />}
        {activeTab === 'email-api' && <EmailApiConfig />}
        {activeTab === 'forward-token' && <ForwardTokenList />}
        {activeTab === 'casdoor' &&
          (canManageCasdoor ? (
            <CasdoorConfigurationForm />
          ) : (
            <p role={isPending ? 'status' : 'alert'}>
              {t(
                ($) =>
                  $[
                    isPending ? 'systemManage.casdoor.loading' : 'systemManage.common.noPermission'
                  ],
                { ns: 'extend' },
              )}
            </p>
          ))}
      </div>
    </div>
  )
}

export default SystemIntegrationPage
