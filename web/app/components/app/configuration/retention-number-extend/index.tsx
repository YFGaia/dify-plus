'use client'
import type { FC } from 'react'
import * as React from 'react'
import { useTranslation } from 'react-i18next'
import { useContext } from 'use-context-selector'
import { Webhooks } from '@/app/components/base/icons/src/vender/line/development'
import Input from '@/app/components/base/input'
import Slider from '@/app/components/base/slider'
import Switch from '@/app/components/base/switch'
import DebugConfigurationContext from '@/context/debug-configuration'

// Extend: 记忆上下文功能
const RetentionNumber: FC = () => {
  const { t } = useTranslation()

  const {
    retentionNumber,
    setRetentionNumber,
  } = useContext(DebugConfigurationContext)

  const defaultCount = Number(process.env.NEXT_CONTEXT_RETENTION_DEFAULT_COUNT || 5) // Extend: 记忆上下文功能
  const maxCount = Number(process.env.NEXT_CONTEXT_RETENTION_MAX_COUNT || 20) // Extend: 记忆上下文功能
  const minCount = Number(process.env.NEXT_CONTEXT_RETENTION_MIN_COUNT || 1) // Extend: 记忆上下文功能

  return (
    <div className="mt-2 rounded-xl border-l-[0.5px] border-t-[0.5px] bg-background-section-burn pb-3">
      {/* Header */}
      <div className="px-3 pt-2">
        <div className="flex h-8 items-center justify-between">
          <div className="flex shrink-0 items-center space-x-1">
            <div className="flex h-6 w-6 items-center justify-center">
              <Webhooks className="text-orange-500" />
            </div>
            <div className="text-text-secondary system-sm-semibold">{t('nodes.common.memory.memory', { ns: 'workflow' })}</div>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex h-8 items-center space-x-2">
              <Switch
                value={retentionNumber !== 999}
                onChange={(v) => {
                  setRetentionNumber(v ? defaultCount : 999)
                }}
              />
              {
                (retentionNumber !== 999) && (
                  <>
                    <Slider
                      className="w-[144px]"
                      value={(retentionNumber || defaultCount) as number}
                      min={minCount}
                      max={maxCount}
                      step={1}
                      onChange={(e) => {
                        setRetentionNumber(Number(e))
                      }}
                    />
                    <Input
                      value={(retentionNumber || defaultCount) as number}
                      wrapperClassName="w-12"
                      className="appearance-none pr-0"
                      type="number"
                      min={minCount}
                      max={maxCount}
                      step={1}
                      onChange={(e) => {
                        setRetentionNumber(Number(e.target.value))
                      }}
                    />
                  </>
                )
              }
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
export default React.memo(RetentionNumber)
