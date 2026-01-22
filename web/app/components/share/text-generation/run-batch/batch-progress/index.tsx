'use client'
import type { FC } from 'react'
import React, { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  RiCheckLine,
  RiErrorWarningLine,
  RiLoader2Line,
  RiPauseLine,
  RiPlayLargeLine,
  RiRefreshLine,
  RiStopLine,
} from '@remixicon/react'
import { resumeBatchApi, retryFailedTasksApi, stopBatchApi } from '@/service/web-extend' // extend: 批量运行工单
import type { BatchStatus } from '@/utils/batch-progress-manager' // extend: 批量运行工单
import ActionButton from '@/app/components/base/action-button'

import { cn } from '@/utils/classnames'

export type BatchProgressProps = {
  batchId: string
  fileName: string
  workflowId?: string
  jobData: {
    id: string
    fileName: string
    createdAt: string
    status: string
    totalRows: number
    processedRows: number
    error?: string
  }
  onDownload: () => void
  onRetrySuccess?: () => void
}

const BatchProgress: FC<BatchProgressProps> = ({
  batchId,
  fileName,
  workflowId,
  jobData,
  onDownload,
  onRetrySuccess,
}) => {
  const { t } = useTranslation()

  const [isLoading, setIsLoading] = useState(false)

  // 停止批量处理
  const handleStop = async () => {
    setIsLoading(true)
    try {
      const success = await stopBatchApi(batchId)
      if (success) {
        // 通知父组件刷新列表
        onRetrySuccess?.()
      }
    }
    catch (error) {
      console.error('Failed to stop batch:', error)
    }
    finally {
      setIsLoading(false)
    }
  }

  // 恢复批量处理
  const handleResume = async () => {
    setIsLoading(true)
    try {
      const success = await resumeBatchApi(batchId)
      if (success) {
        // 通知父组件刷新列表
        onRetrySuccess?.()
      }
    }
    catch (error) {
      console.error('Failed to resume batch:', error)
    }
    finally {
      setIsLoading(false)
    }
  }

  // 重试失败任务（仅重试失败的任务，保留已完成的任务）
  const handleRetry = async () => {
    setIsLoading(true)
    try {
      const success = await retryFailedTasksApi(batchId)
      if (success) {
        // 通知父组件刷新列表
        onRetrySuccess?.()
      }
    }
    catch (error) {
      console.error('Failed to retry failed tasks:', error)
    }
    finally {
      setIsLoading(false)
    }
  }

  const getStatusText = (status: BatchStatus) => {
    switch (status) {
      case 'pending':
        return t('batchWorkflow.pending', { ns: 'extend'})
      case 'processing':
        return t('batchWorkflow.processing', { ns: 'extend'})
      case 'completed':
        return t('batchWorkflow.completed', { ns: 'extend'})
      case 'failed':
        return t('batchWorkflow.failed', { ns: 'extend'})
      case 'stopped':
        return t('batchWorkflow.stopped', { ns: 'extend'})
      default:
        return t('batchWorkflow.pending', { ns: 'extend'})
    }
  }

  const getStatusColor = (status: BatchStatus) => {
    switch (status) {
      case 'pending':
        return 'text-gray-500' // Extend: 批量运行工单
      case 'processing':
        return 'text-blue-700' // Extend: 批量运行工单
      case 'completed':
        return 'text-green-500'
      case 'failed':
        return 'text-red-500'
      case 'stopped':
        return 'text-yellow-500'
      default:
        return 'text-gray-500' // Extend: 批量运行工单
    }
  }

  const formatDate = (dateString: string) => {
    if (!dateString) return '-'

    const date = new Date(dateString)
    // 检查日期是否有效
    if (isNaN(date.getTime()))
      return '-'

    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const currentTime = new Date().toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })

  // 计算进度
  const progress = jobData.totalRows > 0 ? (jobData.processedRows / jobData.totalRows) * 100 : 0
  const status = jobData.status as BatchStatus
  const failed_count = 0 // 从列表API没有这个字段，如果需要可以后续添加

  const getBorderColor = (status: BatchStatus) => {
    switch (status) {
      case 'pending':
        return 'border-gray-300'
      case 'processing':
        return 'border-blue-500'
      case 'completed':
        return 'border-green-500'
      case 'failed':
        return 'border-red-500'
      case 'stopped':
        return 'border-yellow-500'
      default:
        return 'border-gray-300'
    }
  }

  return (
    <div className="space-y-4">
      {/* 统一的批量任务信息框 */}
      <div className={cn('rounded-lg border p-4', getBorderColor(status))}>
        {/* 文件信息 */}
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-gray-900">{t('batchWorkflow.uploadedFileName', { ns: 'extend' })}</div>
            <div className="mt-1 text-xs text-gray-500">{t('batchWorkflow.uploadTime', { ns: 'extend' })}</div>
          </div>
          <div>
            <div className="text-right">
              <div className="text-sm font-medium text-gray-900">{fileName}</div>
              <div className="mt-1 text-xs text-gray-500">{formatDate(jobData.createdAt)}</div>
            </div>
          </div>
        </div>

        {/* 进度条 */}
        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center space-x-2">
              {status === 'processing' && <RiLoader2Line className="h-4 w-4 animate-spin text-blue-500" />}
              {status === 'completed' && <RiCheckLine className="h-4 w-4 text-green-500" />}
              {status === 'failed' && <RiErrorWarningLine className="h-4 w-4 text-red-500" />}
              {status === 'pending' && <RiLoader2Line className="h-4 w-4 text-gray-500" />}
              {status === 'stopped' && <RiPauseLine className="h-4 w-4 text-yellow-500" />}
              <span className={cn('text-sm font-medium', getStatusColor(status))}>
                {getStatusText(status)}
              </span>
            </div>
            <span className={cn('text-sm font-medium', getStatusColor(status))}>
              {isNaN(progress) ? '0' : Math.round(progress)}%
            </span>
          </div>

          {/* 进度条可视化 */}
          <div className="h-2 w-full rounded-full bg-gray-200">
            <div
              className={cn(
                'h-2 rounded-full transition-all duration-300',
                status === 'completed' ? 'bg-green-500'
                  : status === 'processing' ? 'bg-blue-700'
                    : status === 'failed' ? 'bg-red-500'
                      : status === 'stopped' ? 'bg-yellow-500' : 'bg-gray-400',
              )}
              style={{ width: `${Math.min(100, Math.max(0, isNaN(progress) ? 0 : progress))}%` }}
            />
          </div>

          {/* 详细进度信息 */}
          {jobData.totalRows > 0 && (
            <div className="mt-2 text-xs text-gray-500">
              {t('batchWorkflow.processed', {
                processed: jobData.processedRows || 0,
                total: jobData.totalRows || 0,
                ns: 'extend',
              })}
            </div>
          )}

          {/* 错误信息显示 */}
          {jobData.error && status === 'failed' && (
            <div className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3">
              <div className="flex items-start space-x-2">
                <RiErrorWarningLine className="h-4 w-4 text-red-500 mt-0.5 flex-shrink-0" />
                <div className="flex-1">
                  <div className="text-sm font-medium text-red-800 mb-1">
                    {t('batchWorkflow.errorOccurred', { ns: 'extend'} )}
                  </div>
                  <div className="text-xs text-red-700 break-words">
                    {jobData.error}
                  </div>
                </div>
              </div>
            </div>
          )}

        </div>

        {/* 操作按钮区域 */}
        <div className="mt-4 flex items-center justify-between">
          <div className="flex space-x-2">
            {/* 控制按钮 */}
            {(status === 'processing' || status === 'pending') && (
              <ActionButton onClick={handleStop} disabled={isLoading} size="sm">
                {isLoading ? (
                  <RiLoader2Line className="h-4 w-4 animate-spin" />
                ) : (
                  <RiStopLine className="h-4 w-4" />
                )}
                <span className="ml-1">{t('batchWorkflow.stop', { ns: 'extend'})}</span>
              </ActionButton>
            )}
            {status === 'stopped' && (
              <ActionButton onClick={handleResume} disabled={isLoading} size="sm">
                {isLoading ? (
                  <RiLoader2Line className="h-4 w-4 animate-spin" />
                ) : (
                  <RiPlayLargeLine className="h-4 w-4" />
                )}
                <span className="ml-1">{t('batchWorkflow.resume', { ns: 'extend'})}</span>
              </ActionButton>
            )}
            {(status === 'failed') && (
              <ActionButton onClick={handleRetry} disabled={isLoading} size="sm">
                {isLoading ? (
                  <RiLoader2Line className="h-4 w-4 animate-spin" />
                ) : (
                  <RiRefreshLine className="h-4 w-4" />
                )}
                <span className="ml-1">{t('batchWorkflow.retry', { ns: 'extend'})}</span>
              </ActionButton>
            )}
          </div>

          <div className="flex space-x-2">
            {/* 下载按钮 */}
            {(status === 'failed' || status === 'completed' || (status === 'processing' && progress >= 100)) && (
              <ActionButton onClick={onDownload} size="sm">
                <span>{t('batchWorkflow.download', { ns: 'extend'})}</span>
              </ActionButton>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default React.memo(BatchProgress)
