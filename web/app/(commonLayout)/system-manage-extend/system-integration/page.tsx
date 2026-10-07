'use client'

import { parseAsStringLiteral, useQueryState } from 'nuqs'
import { useTranslation } from 'react-i18next'
import { CasdoorConfigurationForm } from '@/features/casdoor/configuration-form'
import { useCasdoorManagementAccess } from '@/features/casdoor/management-access/use-casdoor-management-access'
import { useSystemManagementAccess } from '@/features/system-management/access'
import DingTalkConfig from './dingtalk-config'
import OAuth2Config from './oauth2-config'

type Tab = 'casdoor' | 'dingtalk' | 'oauth2'
const tabParser = parseAsStringLiteral(['casdoor', 'dingtalk', 'oauth2'])
  .withDefault('casdoor')
  .withOptions({ history: 'push', clearOnDefault: false })

const SystemIntegrationPage = () => {
  const { t } = useTranslation()
  const { canManageSystem } = useSystemManagementAccess()
  const { canManageCasdoor, isPending } = useCasdoorManagementAccess()
  const [activeTab, setActiveTab] = useQueryState('tab', tabParser)

  if (!canManageSystem) {
    return <p role="alert">{t(($) => $['systemManage.common.noPermission'], { ns: 'extend' })}</p>
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'casdoor', label: t(($) => $['systemManage.casdoor.title'], { ns: 'extend' }) },
    { key: 'dingtalk', label: t(($) => $['systemManage.dingtalk.title'], { ns: 'extend' }) },
    { key: 'oauth2', label: t(($) => $['systemManage.oauth2.title'], { ns: 'extend' }) },
  ]

  return (
    <div>
      <h1 className="mb-6 text-xl font-semibold text-text-primary">
        {t(($) => $['systemManage.integration.title'], { ns: 'extend' })}
      </h1>

      {/* Tab 切换 */}
      <div className="mb-6 flex border-b border-divider-subtle">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            aria-pressed={activeTab === tab.key}
            onClick={() => {
              void setActiveTab(tab.key)
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
      </div>

      {/* Tab 内容 */}
      <div>
        {activeTab === 'dingtalk' && <DingTalkConfig />}
        {activeTab === 'oauth2' && <OAuth2Config />}
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
