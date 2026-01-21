
'use client'
import React from 'react'
import type { FC } from 'react'
import { useContext } from 'use-context-selector'
import { useTranslation } from 'react-i18next'
import { Webhooks } from '@/app/components/base/icons/src/vender/line/development'
import DebugConfigurationContext from '@/context/debug-configuration'
import Slider from '@/app/components/base/slider'
import Input from '@/app/components/base/input'
import Switch from '@/app/components/base/switch'

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
    <div className={'rounded-xl border-t-[0.5px] border-l-[0.5px] bg-background-section-burn pb-3 mt-2'}>
      {/* Header */}
      <div className='px-3 pt-2'>
        <div className='flex justify-between items-center h-8'>
          <div className='flex items-center space-x-1 shrink-0'>
            <div className='flex items-center justify-center w-6 h-6'>
              <Webhooks className='text-orange-500'/>
            </div>
            <div className='text-text-secondary system-sm-semibold'>{t('nodes.common.memory.memory', { ns: 'workflow' })}</div>
          </div>
          <div className='flex gap-2 items-center'>
            <div className='flex items-center h-8 space-x-2'>
              <Switch
                defaultValue={retentionNumber !== 999}
                onChange={(v) => {
                  setRetentionNumber(v ? defaultCount : 999)
                }}
              />
              {
                (retentionNumber !== 999) && (
                  <>
                    <Slider
                      className='w-[144px]'
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
                      wrapperClassName='w-12'
                      className='pr-0 appearance-none'
                      type='number'
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
