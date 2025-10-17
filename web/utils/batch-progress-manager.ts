/**
 * 全局批量进度轮询管理器
 * 解决多个BatchProgress组件同时轮询导致的性能问题
 */

import { fetchProgressApi } from '@/service/web-extend'

export type BatchStatus = 'pending' | 'processing' | 'completed' | 'failed' | 'stopped'

export type BatchProgressData = {
  id: string
  status: BatchStatus
  total_rows: number
  processed_rows: number
  progress: number
  pending_count: number
  running_count: number
  completed_count: number
  failed_count: number
  error?: string // 添加错误信息字段
  created_at: string
  updated_at: string
}

type BatchProgressListener = (data: BatchProgressData | null) => void

class BatchProgressManager {
  private subscribers: Map<string, Set<BatchProgressListener>> = new Map()
  private pollingTimer: NodeJS.Timeout | null = null
  private pollingInterval = 3000 // 3秒轮询间隔
  private lastProgressData: Map<string, BatchProgressData> = new Map()
  private forcePollingBatches: Map<string, number> = new Map() // batchId -> until timestamp

  private static instance: BatchProgressManager | null = null

  public static getInstance(): BatchProgressManager {
    if (!BatchProgressManager.instance)
      BatchProgressManager.instance = new BatchProgressManager()

    return BatchProgressManager.instance
  }

  private constructor() {
    // 页面可见性变化时调整轮询频率
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          this.adjustPollingInterval(5000) // 页面可见时正常轮询
          this.pollAllBatches() // 立即轮询一次
        }
      })
    }
  }

  /**
   * 订阅批量任务进度更新
   */
  public subscribe(batchId: string, listener: BatchProgressListener): () => void {
    if (!this.subscribers.has(batchId))
      this.subscribers.set(batchId, new Set())

    this.subscribers.get(batchId)!.add(listener)

    // 如果有运行时缓存数据，立即通知
    const cachedData = this.lastProgressData.get(batchId)
    if (cachedData)
      listener(cachedData)

    // 开始轮询
    this.startPolling()

    // 返回取消订阅函数
    return () => {
      this.unsubscribe(batchId, listener)
    }
  }

  /**
   * 取消订阅
   */
  public unsubscribe(batchId: string, listener: BatchProgressListener): void {
    const listeners = this.subscribers.get(batchId)
    if (listeners) {
      listeners.delete(listener)
      if (listeners.size === 0) {
        this.subscribers.delete(batchId)
        this.lastProgressData.delete(batchId)
        this.forcePollingBatches.delete(batchId)
      }
    }

    // 如果没有订阅者了，停止轮询
    if (this.subscribers.size === 0)
      this.stopPolling()
  }

  /**
   * 强制轮询特定批量任务一段时间（用于重试/恢复后）
   */
  public forcePolling(batchId: string, durationMs: number = 15000): void {
    this.forcePollingBatches.set(batchId, Date.now() + durationMs)
  }

  /**
   * 立即获取特定批量任务的进度
   */
  public async fetchProgress(batchId: string): Promise<BatchProgressData | null> {
    try {
      const data = await fetchProgressApi(batchId)
      if (data) {
        // 验证和修复数据
        const sanitizedData = this.sanitizeProgressData(data, batchId)
        this.lastProgressData.set(batchId, sanitizedData)
        this.notifyListeners(batchId, sanitizedData)
        return sanitizedData
      }
      return data
    }
    catch (error) {
      console.error(`获取批量任务 ${batchId} 进度失败:`, error)
      return null
    }
  }

  /**
   * 验证和修复进度数据
   */
  private sanitizeProgressData(data: any, batchId: string): BatchProgressData {
    // 确保数字字段的有效性
    const total_rows = isNaN(Number(data.total_rows)) ? 0 : Number(data.total_rows)
    const processed_rows = isNaN(Number(data.processed_rows)) ? 0 : Number(data.processed_rows)
    const pending_count = isNaN(Number(data.pending_count)) ? 0 : Number(data.pending_count)
    const running_count = isNaN(Number(data.running_count)) ? 0 : Number(data.running_count)
    const completed_count = isNaN(Number(data.completed_count)) ? 0 : Number(data.completed_count)
    const failed_count = isNaN(Number(data.failed_count)) ? 0 : Number(data.failed_count)

    // 计算进度百分比
    let progress = 0
    if (total_rows > 0)
      progress = (processed_rows / total_rows) * 100

    if (isNaN(progress))
      progress = 0

    // 确保日期字段的有效性
    let created_at = data.created_at
    let updated_at = data.updated_at

    if (!created_at || isNaN(new Date(created_at).getTime()))
      created_at = new Date().toISOString()

    if (!updated_at || isNaN(new Date(updated_at).getTime()))
      updated_at = new Date().toISOString()

    return {
      id: data.id || batchId,
      status: data.status || 'pending',
      total_rows,
      processed_rows,
      progress,
      pending_count,
      running_count,
      completed_count,
      failed_count,
      error: data.error || undefined, // 添加错误信息字段
      created_at,
      updated_at,
    }
  }

  /**
   * 调整轮询间隔
   */
  private adjustPollingInterval(interval: number): void {
    if (this.pollingInterval !== interval) {
      this.pollingInterval = interval
      if (this.pollingTimer) {
        this.stopPolling()
        this.startPolling()
      }
    }
  }

  /**
   * 开始轮询
   */
  private startPolling(): void {
    if (this.pollingTimer || this.subscribers.size === 0)
      return

    this.pollingTimer = setInterval(() => {
      this.pollAllBatches()
    }, this.pollingInterval)

    // 立即执行一次轮询
    this.pollAllBatches()
  }

  /**
   * 停止轮询
   */
  private stopPolling(): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer)
      this.pollingTimer = null
    }
  }

  /**
   * 轮询所有需要更新的批量任务
   */
  private async pollAllBatches(): Promise<void> {
    const now = Date.now()
    const batchesToPoll: string[] = []

    for (const [batchId] of this.subscribers) {
      // 检查是否是已完成/失败的缓存任务，如果是则跳过轮询
      const completedCachedData = this.cachedCompletedTasks.get(batchId)
      if (completedCachedData && (completedCachedData.status === 'completed' || completedCachedData.status === 'failed'))
        continue // 跳过已完成和已失败的任务，减少不必要的请求

      const lastData = this.lastProgressData.get(batchId)
      const forceUntil = this.forcePollingBatches.get(batchId)

      // 决定是否需要轮询
      const shouldForcePoll = forceUntil && now < forceUntil
      const shouldPollForActiveStatus = lastData?.status === 'processing' || lastData?.status === 'pending'
      const shouldPollForNoData = !lastData
      // 跳过已完成和已失败的任务（除非强制轮询）
      const shouldSkipCompletedTasks = lastData?.status === 'completed' || lastData?.status === 'failed'

      if ((shouldForcePoll || shouldPollForNoData || shouldPollForActiveStatus) && !shouldSkipCompletedTasks)
        batchesToPoll.push(batchId)

      // 清理过期的强制轮询
      if (forceUntil && now >= forceUntil)
        this.forcePollingBatches.delete(batchId)
    }

    // 批量获取进度 - 这里可以考虑后端提供批量API以进一步优化
    if (batchesToPoll.length > 0) {
      // 为了避免同时发送太多请求，分批处理
      const batchSize = 10
      for (let i = 0; i < batchesToPoll.length; i += batchSize) {
        const batch = batchesToPoll.slice(i, i + batchSize)
        await Promise.all(batch.map(batchId => this.fetchProgress(batchId)))

        // 在批次之间稍微延迟，避免请求过于密集
        if (i + batchSize < batchesToPoll.length)
          await new Promise(resolve => setTimeout(resolve, 100))
      }
    }
  }

  /**
   * 通知监听器
   */
  private notifyListeners(batchId: string, data: BatchProgressData | null): void {
    const listeners = this.subscribers.get(batchId)
    if (listeners) {
      listeners.forEach((listener) => {
        try {
          listener(data)
        }
        catch (error) {
          console.error('批量进度监听器错误:', error)
        }
      })
    }
  }

  /**
   * 获取当前订阅的批量任务数量（用于调试）
   */
  public getSubscribedBatchCount(): number {
    return this.subscribers.size
  }

  /**
   * 获取当前轮询间隔（用于调试）
   */
  public getPollingInterval(): number {
    return this.pollingInterval
  }

  /**
   * 清理特定任务的完成缓存（公开方法，供组件调用）
   */
  public clearCompletedTaskCache(batchId: string): void {
    this.removeFromCompletedCache(batchId)
  }

  /**
   * 检查任务是否已缓存为完成状态
   */
  public isTaskCompleted(batchId: string): boolean {
    const cachedData = this.cachedCompletedTasks.get(batchId)
    return cachedData ? (cachedData.status === 'completed' || cachedData.status === 'failed') : false
  }
}

export default BatchProgressManager.getInstance()
