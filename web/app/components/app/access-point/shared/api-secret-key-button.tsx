'use client'

import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { skipToken, useQuery } from '@tanstack/react-query'
import { useAtomValue } from 'jotai'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiKeyModal } from '@/app/components/api-key/api-key-modal'
import { isCurrentWorkspaceManagerAtom } from '@/context/workspace-state'
import { consoleQuery } from '@/service/console'

type ApiSecretKeyButtonProps = {
  appId: string
  canManage: boolean
  apiKeyCount?: number
  disabled?: boolean
  environmentId?: string
}

export function ApiSecretKeyButton({
  appId,
  canManage,
  apiKeyCount: environmentApiKeyCount,
  disabled = false,
  environmentId,
}: ApiSecretKeyButtonProps) {
  const { t } = useTranslation()
  const [modalOpen, setModalOpen] = useState(false)
  const isCurrentWorkspaceManager = useAtomValue(isCurrentWorkspaceManagerAtom)
  const canManageApiKeys = isCurrentWorkspaceManager && canManage
  const isEnvironmentScope = Boolean(environmentId)
  const apiKeysQuery = useQuery(
    consoleQuery.apps.byResourceId.apiKeys.get.queryOptions({
      input:
        isEnvironmentScope || !canManageApiKeys ? skipToken : { params: { resource_id: appId } },
    }),
  )
  const apiKeyCount = isEnvironmentScope
    ? (environmentApiKeyCount ?? 0)
    : (apiKeysQuery.data?.data.length ?? 0)
  const buttonDisabled =
    disabled ||
    !canManageApiKeys ||
    (!isEnvironmentScope && (apiKeysQuery.isPending || apiKeysQuery.isError))

  if (!isCurrentWorkspaceManager) return null

  return (
    <>
      <Button
        variant="secondary"
        size="medium"
        className="px-3"
        disabled={buttonDisabled}
        onClick={() => setModalOpen(true)}
      >
        <span aria-hidden className="i-ri-key-2-line size-4" />
        {t(($) => $['apiKeyModal.apiSecretKey'], { ns: 'appApi' })}
        <span
          className={cn(
            'flex min-w-4 shrink-0 items-center justify-center rounded-[5px] border border-divider-deep bg-components-badge-bg-dimm px-1 py-0.5 system-2xs-medium-uppercase tabular-nums',
            buttonDisabled ? 'text-text-disabled' : 'text-text-tertiary',
          )}
        >
          {apiKeyCount}
        </span>
      </Button>

      {/* Revoking access also removes any nested key-management dialogs. */}
      {canManageApiKeys && (
        <ApiKeyModal
          canManage={canManageApiKeys}
          open={modalOpen}
          scope={
            environmentId ? { type: 'environment', appId, environmentId } : { type: 'app', appId }
          }
          onOpenChange={setModalOpen}
        />
      )}
    </>
  )
}
