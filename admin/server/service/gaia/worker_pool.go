package gaia

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
)

// UserWorkerAllocation 用户工作器分配信息
type UserWorkerAllocation struct {
	UserID   uint `json:"user_id"`
	Workers  int  `json:"workers"`
	MaxLimit int  `json:"max_limit"`
}

// 全局工作池实例
var globalWorkerPool *WorkerPool

// WorkerPool 工作池管理器
type WorkerPool struct {
	ctx            context.Context
	cancel         context.CancelFunc
	totalWorkers   int                                   // 总工作器数量
	userWorkers    map[uint]*UserWorkerAllocation        // 每个用户的工作器分配
	userTaskChan   map[uint]chan *gaia.BatchWorkflowTask // 每个用户的任务队列
	runningWorkers map[uint]int                          // 每个用户当前运行的worker数量
	wg             sync.WaitGroup
	batchService   *BatchWorkflowService
	running        bool
	mutex          sync.RWMutex
	userMutex      sync.RWMutex
}

// NewWorkerPool 创建新的工作池
func NewWorkerPool(totalWorkers int) *WorkerPool {
	ctx, cancel := context.WithCancel(context.Background())
	return &WorkerPool{
		ctx:            ctx,
		cancel:         cancel,
		totalWorkers:   totalWorkers,
		userWorkers:    make(map[uint]*UserWorkerAllocation),
		userTaskChan:   make(map[uint]chan *gaia.BatchWorkflowTask),
		runningWorkers: make(map[uint]int),
		batchService:   &BatchWorkflowService{},
		running:        false,
	}
}

// calculateWorkerCountWithErrorPenalty 根据错误次数计算工作器数量
// 每50个错误减少1个并发位，最少保留1个并发位
func (wp *WorkerPool) calculateWorkerCountWithErrorPenalty(baseWorkers int, errorCount int) int {
	if baseWorkers <= 0 {
		return 1
	}

	// 计算错误惩罚：每50个错误减少1个并发位
	penalty := errorCount / gaia.ErrorPenaltyThreshold
	adjustedWorkers := baseWorkers - penalty

	// 确保至少保留1个并发位
	if adjustedWorkers < 1 {
		adjustedWorkers = 1
	}

	return adjustedWorkers
}

// calculateUserWorkerAllocation 计算用户工作器分配
func (wp *WorkerPool) calculateUserWorkerAllocation() {
	wp.userMutex.Lock()
	defer wp.userMutex.Unlock()

	// 获取有批量任务的活跃用户（按需分配）
	// 排除超过重试次数的任务，只考虑可以继续处理的任务
	// 分两个查询：1. 获取有活跃任务的用户，2. 获取用户的累计错误次数

	// 第一个查询：获取有活跃批量任务的用户
	var activeUserIDs []uint
	err := global.GVA_DB.Raw(`
		SELECT DISTINCT bw.user_id
		FROM batch_workflows_extend bw
		INNER JOIN sys_users su ON bw.user_id = su.id
		INNER JOIN batch_workflow_tasks_extend bwt ON bw.id = bwt.batch_workflow_id
		WHERE su.enable = ?
		AND bw.status IN (?, ?)
		AND (bwt.status IN (?, ?) AND bwt.error_count < ?)
	`, system.UserActive, gaia.BatchWorkflowStatusPending, gaia.BatchWorkflowStatusProcessing,
		gaia.BatchTaskStatusPending, gaia.BatchTaskStatusQueued, gaia.MaxTaskRetryCount).Scan(&activeUserIDs).Error

	if err != nil {
		global.GVA_LOG.Error("获取有批量任务的活跃用户失败: " + err.Error())
		return
	}

	// 第二个查询：获取这些用户的所有批量工作流的累计错误次数（不限状态）
	type UserErrorInfo struct {
		UserID     uint `json:"user_id"`
		ErrorCount int  `json:"error_count"`
	}
	var userErrorInfos []UserErrorInfo
	if len(activeUserIDs) > 0 {
		err = global.GVA_DB.Raw(`
			SELECT bw.user_id, COALESCE(SUM(bw.error_count), 0) as error_count
			FROM batch_workflows_extend bw
			WHERE bw.user_id IN (?) AND bw.status='pending'
			GROUP BY bw.user_id
		`, activeUserIDs).Scan(&userErrorInfos).Error

		if err != nil {
			global.GVA_LOG.Error("获取用户累计错误次数失败: " + err.Error())
			return
		}
	}

	// 提取活跃用户ID列表和错误次数映射
	userErrorMap := make(map[uint]int)
	for _, info := range userErrorInfos {
		userErrorMap[info.UserID] = info.ErrorCount
	}

	userCount := len(activeUserIDs)
	if userCount == 0 {
		// 如果没有用户有批量任务，关闭所有队列
		for _, ch := range wp.userTaskChan {
			close(ch)
		}
		wp.userWorkers = make(map[uint]*UserWorkerAllocation)
		wp.userTaskChan = make(map[uint]chan *gaia.BatchWorkflowTask)
		wp.runningWorkers = make(map[uint]int)
		return
	}

	// 创建活跃用户ID集合
	activeUserIDMap := make(map[uint]bool)
	for _, userID := range activeUserIDs {
		activeUserIDMap[userID] = true
	}

	// 关闭不再有批量任务的用户的任务队列
	for userID, ch := range wp.userTaskChan {
		if !activeUserIDMap[userID] {
			close(ch)
			delete(wp.userTaskChan, userID)
			delete(wp.userWorkers, userID)
			delete(wp.runningWorkers, userID)
		}
	}

	// 检查用户数量是否超过了最大支持数量（每用户最少1个工作器）
	maxSupportedUsers := wp.totalWorkers / 1

	// 存储新的分配计算结果
	newAllocations := make(map[uint]*UserWorkerAllocation)

	if userCount <= maxSupportedUsers {
		// 用户数量在可支持范围内，采用两阶段分配策略
		baseAllocation := wp.totalWorkers / userCount
		remainder := wp.totalWorkers % userCount

		// 第一阶段：计算每个用户的基础分配和错误惩罚后的实际分配
		type UserAllocationInfo struct {
			UserID         uint
			BaseWorkers    int
			ActualWorkers  int
			ErrorCount     int
			PenaltyReduced int
		}

		var userAllocations []UserAllocationInfo
		totalPenaltyReduced := 0

		for i, userID := range activeUserIDs {
			baseWorkers := baseAllocation
			// 处理余数，前几个用户多分配一个
			if i < remainder {
				baseWorkers++
			}

			// 确保每个用户至少有1个并发位
			if baseWorkers < 1 {
				baseWorkers = 1
			}

			// 应用错误惩罚：根据用户的累计错误次数减少并发位
			errorCount := userErrorMap[userID]
			actualWorkers := wp.calculateWorkerCountWithErrorPenalty(baseWorkers, errorCount)
			penaltyReduced := baseWorkers - actualWorkers
			totalPenaltyReduced += penaltyReduced
			userAllocations = append(userAllocations, UserAllocationInfo{
				UserID:         userID,
				BaseWorkers:    baseWorkers,
				ActualWorkers:  actualWorkers,
				ErrorCount:     errorCount,
				PenaltyReduced: penaltyReduced,
			})
		}

		// 第二阶段：将空出来的并发位重新分配给错误较少的用户
		if totalPenaltyReduced > 0 {
			// 按错误数量排序，错误少的用户优先获得额外分配
			for i := 0; i < len(userAllocations)-1; i++ {
				for j := i + 1; j < len(userAllocations); j++ {
					if userAllocations[i].ErrorCount > userAllocations[j].ErrorCount {
						userAllocations[i], userAllocations[j] = userAllocations[j], userAllocations[i]
					}
				}
			}

			// 只为没有被惩罚的用户（PenaltyReduced = 0）重新分配空闲的并发位
			// 被惩罚的用户不应该获得额外分配
			remainingToDistribute := totalPenaltyReduced
			eligibleUsers := 0

			// 计算有资格获得额外分配的用户数量（没有被惩罚的用户）
			for _, allocation := range userAllocations {
				if allocation.PenaltyReduced == 0 {
					eligibleUsers++
				}
			}

			if eligibleUsers > 0 {
				// 只为没有被惩罚的用户分配额外的并发位
				for i := 0; i < len(userAllocations) && remainingToDistribute > 0; i++ {
					if userAllocations[i].PenaltyReduced == 0 {
						// 为没有错误惩罚的用户分配额外的并发位
						extraWorkers := remainingToDistribute / eligibleUsers
						if extraWorkers < 1 {
							extraWorkers = 1
						}
						if extraWorkers > remainingToDistribute {
							extraWorkers = remainingToDistribute
						}

						userAllocations[i].ActualWorkers += extraWorkers
						remainingToDistribute -= extraWorkers
						eligibleUsers--
					}
				}
			}
		}

		// 创建最终分配结果
		totalFinalWorkers := 0
		for _, allocation := range userAllocations {
			newAllocations[allocation.UserID] = &UserWorkerAllocation{
				UserID:   allocation.UserID,
				Workers:  allocation.ActualWorkers,
				MaxLimit: wp.totalWorkers,
			}
			totalFinalWorkers += allocation.ActualWorkers
		}
	} else {
		// 用户数量超过最大支持数量，采用降级分配策略（两阶段分配）
		baseAllocation := wp.totalWorkers / userCount
		remainder := wp.totalWorkers % userCount

		// 第一阶段：计算每个用户的基础分配和错误惩罚后的实际分配
		type UserAllocationInfo struct {
			UserID         uint
			BaseWorkers    int
			ActualWorkers  int
			ErrorCount     int
			PenaltyReduced int
		}

		var userAllocations []UserAllocationInfo
		totalPenaltyReduced := 0

		for i, userID := range activeUserIDs {
			baseWorkers := baseAllocation
			// 处理余数，前几个用户多分配一个
			if i < remainder {
				baseWorkers++
			}

			// 确保至少分配1个工作器
			if baseWorkers < 1 {
				baseWorkers = 1
			}

			// 应用错误惩罚：根据用户的累计错误次数减少并发位
			errorCount := userErrorMap[userID]
			actualWorkers := wp.calculateWorkerCountWithErrorPenalty(baseWorkers, errorCount)
			penaltyReduced := baseWorkers - actualWorkers
			totalPenaltyReduced += penaltyReduced

			// 添加详细的错误惩罚计算调试日志
			userAllocations = append(userAllocations, UserAllocationInfo{
				UserID:         userID,
				BaseWorkers:    baseWorkers,
				ActualWorkers:  actualWorkers,
				ErrorCount:     errorCount,
				PenaltyReduced: penaltyReduced,
			})
		}

		// 第二阶段：将空出来的并发位重新分配给错误较少的用户
		if totalPenaltyReduced > 0 {
			// 按错误数量排序，错误少的用户优先获得额外分配
			for i := 0; i < len(userAllocations)-1; i++ {
				for j := i + 1; j < len(userAllocations); j++ {
					if userAllocations[i].ErrorCount > userAllocations[j].ErrorCount {
						userAllocations[i], userAllocations[j] = userAllocations[j], userAllocations[i]
					}
				}
			}

			// 只为没有被惩罚的用户（PenaltyReduced = 0）重新分配空闲的并发位
			// 被惩罚的用户不应该获得额外分配
			remainingToDistribute := totalPenaltyReduced
			eligibleUsers := 0

			// 计算有资格获得额外分配的用户数量（没有被惩罚的用户）
			for _, allocation := range userAllocations {
				if allocation.PenaltyReduced == 0 {
					eligibleUsers++
				}
			}

			if eligibleUsers > 0 {
				// 只为没有被惩罚的用户分配额外的并发位
				for i := 0; i < len(userAllocations) && remainingToDistribute > 0; i++ {
					if userAllocations[i].PenaltyReduced == 0 {
						// 为没有错误惩罚的用户分配额外的并发位
						extraWorkers := remainingToDistribute / eligibleUsers
						if extraWorkers < 1 {
							extraWorkers = 1
						}
						if extraWorkers > remainingToDistribute {
							extraWorkers = remainingToDistribute
						}

						userAllocations[i].ActualWorkers += extraWorkers
						remainingToDistribute -= extraWorkers
						eligibleUsers--
					}
				}
			}
		}

		// 创建最终分配结果，确保不超过总工作器数量
		allocatedWorkers := 0
		for _, allocation := range userAllocations {
			workers := allocation.ActualWorkers

			// 确保不会超过剩余的工作器数量
			remainingWorkers := wp.totalWorkers - allocatedWorkers
			if workers > remainingWorkers {
				workers = remainingWorkers
			}

			if workers > 0 {
				newAllocations[allocation.UserID] = &UserWorkerAllocation{
					UserID:   allocation.UserID,
					Workers:  workers,
					MaxLimit: wp.totalWorkers,
				}
				allocatedWorkers += workers
			}

			// 如果工作器已经分配完毕，剩余用户分配0个工作器
			if allocatedWorkers >= wp.totalWorkers {
				break
			}
		}

		global.GVA_LOG.Warn(fmt.Sprintf("降级分配完成 - 总工作器: %d, 用户数: %d, 已分配: %d, 重新分配: %d, 平均每用户: %.1f个",
			wp.totalWorkers, userCount, allocatedWorkers, totalPenaltyReduced, float64(allocatedWorkers)/float64(userCount)))
	}

	// 应用新的分配，只更新有变化的用户
	for userID, newAllocation := range newAllocations {
		oldAllocation, exists := wp.userWorkers[userID]

		if !exists {
			// 新用户，创建分配和任务队列
			wp.userWorkers[userID] = newAllocation
			wp.userTaskChan[userID] = make(chan *gaia.BatchWorkflowTask, newAllocation.Workers*2)
		} else if oldAllocation.Workers != newAllocation.Workers {
			// 现有用户的工作器数量发生变化，需要重新创建任务队列
			close(wp.userTaskChan[userID])
			wp.userWorkers[userID] = newAllocation
			wp.userTaskChan[userID] = make(chan *gaia.BatchWorkflowTask, newAllocation.Workers*2)
			// 重置运行中的worker计数，让adjustWorkers重新启动
			wp.runningWorkers[userID] = 0
		} else {
			// 工作器数量没有变化，只更新分配信息
			wp.userWorkers[userID] = newAllocation
		}
	}
}

// getUserWorkerCount 获取指定用户的工作器数量
func (wp *WorkerPool) getUserWorkerCount(userID uint) int {
	wp.userMutex.RLock()
	defer wp.userMutex.RUnlock()

	if allocation, exists := wp.userWorkers[userID]; exists {
		return allocation.Workers
	}
	return 0
}

// Start 启动工作池
func (wp *WorkerPool) Start() {
	wp.mutex.Lock()
	defer wp.mutex.Unlock()

	if wp.running {
		return
	}

	// 计算用户工作器分配
	wp.calculateUserWorkerAllocation()

	wp.running = true

	// 启动初始工作器
	wp.startWorkers()

	// 启动任务调度器
	wp.wg.Add(1)
	go wp.taskScheduler()

	// 启动用户工作器分配更新器
	wp.wg.Add(1)
	go wp.userAllocationUpdater()

	// 启动动态工作器管理器
	wp.wg.Add(1)
	go wp.dynamicWorkerManager()
}

// Stop 停止工作池
func (wp *WorkerPool) Stop() {
	wp.mutex.Lock()
	defer wp.mutex.Unlock()

	if !wp.running {
		return
	}

	wp.cancel()
	wp.running = false

	// 关闭所有用户的任务队列
	wp.userMutex.Lock()
	for _, ch := range wp.userTaskChan {
		close(ch)
	}
	wp.userMutex.Unlock()

	// 等待所有goroutine完成
	wp.wg.Wait()
}

// IsRunning 检查工作池是否运行中
func (wp *WorkerPool) IsRunning() bool {
	wp.mutex.RLock()
	defer wp.mutex.RUnlock()
	return wp.running
}

// GetStatus 获取工作池状态
func (wp *WorkerPool) GetStatus() map[string]interface{} {
	wp.mutex.RLock()
	defer wp.mutex.RUnlock()

	wp.userMutex.RLock()
	defer wp.userMutex.RUnlock()

	userAllocations := make(map[string]interface{})
	for userID, allocation := range wp.userWorkers {
		userAllocations[fmt.Sprintf("user_%d", userID)] = allocation
	}

	// 计算所有用户队列的总长度
	totalQueueLength := 0
	userQueueLengths := make(map[string]int)
	for userID, ch := range wp.userTaskChan {
		queueLen := len(ch)
		totalQueueLength += queueLen
		userQueueLengths[fmt.Sprintf("user_%d", userID)] = queueLen
	}

	return map[string]interface{}{
		"running":            wp.running,
		"total_workers":      wp.totalWorkers,
		"total_queue_length": totalQueueLength,
		"user_queue_lengths": userQueueLengths,
		"user_allocations":   userAllocations,
	}
}

// userAllocationUpdater 用户工作器分配更新器
func (wp *WorkerPool) userAllocationUpdater() {
	defer wp.wg.Done()

	ticker := time.NewTicker(30 * time.Second) // 每30秒检查一次用户变化
	defer ticker.Stop()

	for {
		select {
		case <-wp.ctx.Done():
			return
		case <-ticker.C:
			wp.calculateUserWorkerAllocation()
		}
	}
}

// dynamicWorkerManager 动态工作器管理器
func (wp *WorkerPool) dynamicWorkerManager() {
	defer wp.wg.Done()

	defer global.GVA_LOG.Info("动态工作器管理器停止")

	ticker := time.NewTicker(10 * time.Second) // 每10秒检查一次工作器状态
	defer ticker.Stop()

	for {
		select {
		case <-wp.ctx.Done():
			return
		case <-ticker.C:
			wp.adjustWorkers()
		}
	}
}

// startWorkers 启动工作器
func (wp *WorkerPool) startWorkers() {
	wp.userMutex.Lock()
	defer wp.userMutex.Unlock()

	for userID, allocation := range wp.userWorkers {
		// 启动所有需要的worker
		for i := 0; i < allocation.Workers; i++ {
			wp.wg.Add(1)
			workerID := fmt.Sprintf("user_%d_worker_%d", userID, i)
			go wp.worker(workerID, userID)
		}
		// 更新运行中的worker数量
		wp.runningWorkers[userID] = allocation.Workers
	}
}

// adjustWorkers 调整工作器数量
func (wp *WorkerPool) adjustWorkers() {
	// 重新计算用户分配
	wp.calculateUserWorkerAllocation()

	// 检查哪些用户的工作器数量发生了变化，为它们启动新worker
	wp.userMutex.Lock()
	defer wp.userMutex.Unlock()

	for userID, allocation := range wp.userWorkers {
		runningCount := wp.runningWorkers[userID]
		neededCount := allocation.Workers

		if runningCount < neededCount {
			// 需要启动更多worker
			for i := runningCount; i < neededCount; i++ {
				wp.wg.Add(1)
				workerID := fmt.Sprintf("user_%d_worker_%d", userID, i)
				go wp.worker(workerID, userID)
			}
			wp.runningWorkers[userID] = neededCount
		}
	}
}

// worker 工作协程
func (wp *WorkerPool) worker(workerID string, userID uint) {
	defer wp.wg.Done()
	defer func() {
		// Worker退出时减少运行中的worker计数
		wp.userMutex.Lock()
		if wp.runningWorkers[userID] > 0 {
			wp.runningWorkers[userID]--
		}
		wp.userMutex.Unlock()
	}()

	// 获取用户专属的任务队列
	wp.userMutex.RLock()
	userTaskChan, exists := wp.userTaskChan[userID]
	wp.userMutex.RUnlock()

	if !exists {
		global.GVA_LOG.Error(fmt.Sprintf("Worker %s: 用户 %d 的任务队列不存在", workerID, userID))
		return
	}

	for {
		select {
		case <-wp.ctx.Done():
			return
		case task, ok := <-userTaskChan:
			if !ok {
				// 任务队列已关闭
				return
			}
			if task != nil {
				wp.processTask(task)
			}
		}
	}
}

// taskScheduler 任务调度器
func (wp *WorkerPool) taskScheduler() {
	defer wp.wg.Done()
	ticker := time.NewTicker(2 * time.Second) // 每2秒检查一次新任务
	defer ticker.Stop()

	for {
		select {
		case <-wp.ctx.Done():
			return
		case <-ticker.C:
			global.GVA_LOG.Debug("任务调度器开始检查新任务...")
			wp.fetchAndScheduleTasks()
		}
	}
}

// fetchAndScheduleTasks 获取并调度任务
func (wp *WorkerPool) fetchAndScheduleTasks() {
	global.GVA_LOG.Debug("fetchAndScheduleTasks 开始执行")

	if global.GVA_DB == nil {
		global.GVA_LOG.Error("数据库连接为空，无法获取任务")
		return
	}

	// 获取所有待处理的任务，但排除已停止的批量工作流的任务和超过重试次数的任务
	var tasks []gaia.BatchWorkflowTask
	err := global.GVA_DB.Table("batch_workflow_tasks_extend bwt").
		Select("bwt.*").
		Joins("INNER JOIN batch_workflows_extend bw ON bwt.batch_workflow_id = bw.id").
		Where("bwt.status = ? AND bw.status != ? AND bwt.error_count < ?", gaia.BatchTaskStatusPending, gaia.BatchWorkflowStatusStopped, gaia.MaxTaskRetryCount).
		Order("bwt.created_at ASC").
		Find(&tasks).Error

	if err != nil {
		global.GVA_LOG.Error("获取待处理任务失败: " + err.Error())
		return
	}

	// 按用户分组任务
	userTasks := make(map[uint][]*gaia.BatchWorkflowTask)
	for i := range tasks {
		task := &tasks[i]
		// 获取任务对应的用户ID
		var batchWorkflow gaia.BatchWorkflow
		if err = global.GVA_DB.Where("id = ?", task.BatchWorkflowID).First(&batchWorkflow).Error; err != nil {
			global.GVA_LOG.Error(fmt.Sprintf("找不到任务 %s 对应的批量工作流 %s: %s", task.ID, task.BatchWorkflowID, err.Error()))
			continue
		}
		userTasks[batchWorkflow.UserID] = append(userTasks[batchWorkflow.UserID], task)
	}

	// 在分配任务前，再次清理已停止的批量工作流任务
	cleanupStoppedBatchWorkflowTasks()

	// 为每个用户分配任务到队列
	for userID, userTaskList := range userTasks {
		userWorkerCount := wp.getUserWorkerCount(userID)
		if userWorkerCount == 0 {
			continue
		}

		// 限制任务数量
		if len(userTaskList) > userWorkerCount {
			userTaskList = userTaskList[:userWorkerCount]
		}

		// 获取用户专属的任务队列
		wp.userMutex.RLock()
		userTaskChan, exists := wp.userTaskChan[userID]
		wp.userMutex.RUnlock()

		if !exists {
			continue
		}

		// 将任务标记为排队状态并加入队列
		for _, task := range userTaskList {
			// 更新任务状态为排队中
			if err = global.GVA_DB.Model(task).Update("status", gaia.BatchTaskStatusQueued).Error; err != nil {
				global.GVA_LOG.Error(fmt.Sprintf("更新任务状态失败: %s", err.Error()))
				continue
			}

			// 非阻塞方式添加到用户专属队列
			select {
			case userTaskChan <- task:

				//global.GVA_LOG.Info(fmt.Sprintf("成功将任务 %s 添加到用户 %d 的队列", task.ID, userID))
			case <-wp.ctx.Done():
				return
			default:
				// 队列满了，将任务状态改回pending
				//global.GVA_LOG.Warn(fmt.Sprintf("用户 %d 的队列已满，任务 %s 状态改回pending", userID, task.ID))
				global.GVA_DB.Model(task).Update("status", gaia.BatchTaskStatusPending)
			}
		}

		if len(userTaskList) > 0 {
			global.GVA_LOG.Info(fmt.Sprintf("为用户 %d 调度了 %d 个任务到队列", userID, len(userTaskList)))
		}
	}
}

// processTask 处理单个任务
func (wp *WorkerPool) processTask(task *gaia.BatchWorkflowTask) {
	// 更新任务状态为运行中
	if err := global.GVA_DB.Model(task).Updates(map[string]interface{}{
		"status":     gaia.BatchTaskStatusRunning,
		"updated_at": time.Now(),
	}).Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新任务状态失败: %s", err.Error()))
		return
	}

	// 获取批量工作流信息
	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", task.BatchWorkflowID).First(&batchWorkflow).Error; err != nil {
		wp.updateTaskError(task, "获取批量工作流信息失败: "+err.Error())
		return
	}

	// 检查批量工作流是否被停止
	if batchWorkflow.Status == gaia.BatchWorkflowStatusStopped {
		wp.updateTaskError(task, "批量工作流已被停止")
		return
	}

	// 解析输入参数
	var inputs map[string]string
	if err := json.Unmarshal([]byte(task.Inputs), &inputs); err != nil {
		wp.updateTaskError(task, "解析输入参数失败: "+err.Error())
		return
	}

	// 检查输入参数是否全为空值
	hasNonEmptyValue := false
	for _, value := range inputs {
		if strings.TrimSpace(value) != "" {
			hasNonEmptyValue = true
			break
		}
	}

	// 如果所有输入都为空，跳过处理并标记为完成
	if !hasNonEmptyValue {
		global.GVA_LOG.Info(fmt.Sprintf("任务 %s 包含全空值输入，跳过处理并标记为完成", task.ID))

		// 创建空结果并标记为完成
		emptyResultJSON, _ := json.Marshal(map[string]interface{}{
			"status":  gaia.BatchTaskStatusCompleted,
			"message": "跳过空值输入任务",
			"outputs": map[string]interface{}{
				"text": "输入为空，已跳过处理",
			},
		})

		// 更新任务状态为完成
		if err := global.GVA_DB.Model(task).Updates(map[string]interface{}{
			"status":     gaia.BatchTaskStatusCompleted,
			"result":     string(emptyResultJSON),
			"updated_at": time.Now(),
		}).Error; err != nil {
			global.GVA_LOG.Error(fmt.Sprintf("更新空值任务状态失败: %s", err.Error()))
			return
		}

		// 更新批量处理的已处理行数
		global.GVA_DB.Exec("UPDATE batch_workflows_extend SET processed_rows = processed_rows + 1, updated_at = ? WHERE id = ?",
			time.Now(), batchWorkflow.ID)

		// 检查批量工作流是否完成
		wp.checkBatchWorkflowCompletion(batchWorkflow.ID)
		return
	}

	// 快速生成即时token和CSRF token
	var err error
	var token string
	var csrfToken string
	var user system.SysUser
	if err = global.GVA_DB.Where(
		"id = ? AND enable = ?", batchWorkflow.UserID, system.UserActive).First(&user).Error; err != nil {
		wp.updateTaskError(task, "用户不存在: "+err.Error())
		return
	}
	// 生成这个用户的token和CSRF token
	if token, csrfToken, _, err = utils.LoginTokenWithCSRF(&user); err != nil {
		wp.updateTaskError(task, "用户token生成失败: "+err.Error())
		return
	}

	// 调用Dify API
	result, err := wp.batchService.callDifyAPI(batchWorkflow.InstalledID, token, csrfToken, inputs)
	if err != nil {
		// 检查是否是余额不足错误（403状态码）
		if strings.Contains(err.Error(), "状态码: 403") && strings.Contains(
			err.Error(), "Insufficient balance") {
			global.GVA_LOG.Warn(fmt.Sprintf(
				"用户 %d 余额不足，将其所有pending和processing状态的批量工作流和任务设置为失败", batchWorkflow.UserID))
			wp.handleInsufficientBalance(batchWorkflow.UserID, task.BatchWorkflowID)
			wp.updateTaskError(task, gaia.ErrorInsufficientBalance)
			return
		}
		wp.updateTaskError(task, gaia.ErrorCallAPIFailed+": "+err.Error())
		return
	}

	// 解析返回结果，检查是否有错误
	var apiResult map[string]interface{}
	if err = json.Unmarshal([]byte(result), &apiResult); err != nil {
		wp.updateTaskError(task, gaia.ErrorParseResultFailed+": "+err.Error())
		return
	}

	// 检查API返回的状态
	if status, ok := apiResult["status"].(string); ok && status == gaia.BatchTaskStatusFailed {
		// API执行失败，提取错误信息
		var apiError string
		errorMsg := gaia.ErrorWorkflowFailed
		if apiError, ok = apiResult["error"].(string); ok && apiError != "" {
			errorMsg = apiError
		}
		// 检查是否是余额不足错误
		if strings.Contains(result, "call failed") || strings.Contains(
			apiError, "Insufficient balance") {
			global.GVA_LOG.Warn(fmt.Sprintf(
				"用户 %d 余额不足，将其所有pending和processing状态的批量工作流和任务设置为失败", batchWorkflow.UserID))
			wp.handleInsufficientBalance(batchWorkflow.UserID, task.BatchWorkflowID)
			wp.updateTaskError(task, gaia.ErrorInsufficientBalance)
			return
		}
		// 其他类型的失败，标记为失败状态
		wp.updateTaskError(task, errorMsg)
		return
	}

	// API执行成功，更新任务结果
	if err = global.GVA_DB.Model(task).Updates(map[string]interface{}{
		"status":     gaia.BatchTaskStatusCompleted,
		"result":     result,
		"updated_at": time.Now(),
	}).Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新任务结果失败: %s", err.Error()))
		return
	}

	// 更新批量处理的已处理行数
	global.GVA_DB.Exec(
		"UPDATE batch_workflows_extend SET processed_rows = processed_rows + 1, updated_at = ? WHERE id = ?",
		time.Now(), batchWorkflow.ID)

	// 检查批量工作流是否完成
	wp.checkBatchWorkflowCompletion(batchWorkflow.ID)
}

// decodeUnicodeEscapes 解码字符串中的 Unicode 转义序列
func decodeUnicodeEscapes(input string) string {
	// 尝试将字符串作为带引号的字符串进行解码
	if decoded, err := strconv.Unquote(`"` + input + `"`); err == nil {
		return decoded
	}

	// 如果直接解码失败，尝试逐个替换 Unicode 转义序列
	// 处理类似 \u897f\u73ed\u7259\u7ad9 这样的转义序列
	result := input
	for {
		// 查找下一个 \u 序列的起始位置
		startIdx := strings.Index(result, "\\u")
		if startIdx == -1 {
			break
		}

		// 检查是否有足够的字符来形成一个完整的 Unicode 转义序列
		if startIdx+6 > len(result) {
			break
		}

		// 提取 Unicode 转义序列（包括 \u 和 4 位十六进制数字）
		unicodeEscape := result[startIdx : startIdx+6]

		// 尝试解码这个单独的 Unicode 转义序列
		if decoded, err := strconv.Unquote(`"` + unicodeEscape + `"`); err == nil {
			// 替换原字符串中的转义序列
			result = result[:startIdx] + decoded + result[startIdx+6:]
		} else {
			// 如果解码失败，跳过这个序列，防止无限循环
			result = result[:startIdx] + "?" + result[startIdx+6:]
		}
	}

	return result
}

// updateTaskError 更新任务错误信息
func (wp *WorkerPool) updateTaskError(task *gaia.BatchWorkflowTask, errorMsg string) {
	// 解码错误信息中的 Unicode 转义序列
	decodedErrorMsg := decodeUnicodeEscapes(errorMsg)
	global.GVA_LOG.Error(fmt.Sprintf("任务 %s 失败: %s", task.ID, decodedErrorMsg))

	// 增加错误次数
	newErrorCount := task.ErrorCount + 1

	// 更新批量工作流的错误次数和错误信息
	if err := global.GVA_DB.Exec(
		"UPDATE batch_workflows_extend SET error_count = error_count + 1, error = ?, updated_at = ? WHERE id = ?",
		decodedErrorMsg, time.Now(), task.BatchWorkflowID).Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新批量工作流错误次数和错误信息失败: %s", err.Error()))
	}

	// 检查是否超过最大重试次数
	if newErrorCount >= gaia.MaxTaskRetryCount {
		// 超过重试次数，标记为最终失败
		if err := global.GVA_DB.Model(task).Updates(map[string]interface{}{
			"status":      gaia.BatchTaskStatusFailed,
			"error":       fmt.Sprintf("%s: %s", gaia.ErrorMaxRetryExceeded, decodedErrorMsg),
			"error_count": newErrorCount,
			"updated_at":  time.Now(),
		}).Error; err != nil {
			global.GVA_LOG.Error(fmt.Sprintf("更新任务最终失败状态失败: %s", err.Error()))
		}
		global.GVA_LOG.Warn(fmt.Sprintf("任务 %s 重试次数已达上限(%d次)，标记为最终失败", task.ID, gaia.MaxTaskRetryCount))
	} else {
		// 未超过重试次数，重置为pending状态以便重试
		if err := global.GVA_DB.Model(task).Updates(map[string]interface{}{
			"status":      gaia.BatchTaskStatusPending,
			"error":       decodedErrorMsg,
			"error_count": newErrorCount,
			"updated_at":  time.Now(),
		}).Error; err != nil {
			global.GVA_LOG.Error(fmt.Sprintf("更新任务重试状态失败: %s", err.Error()))
		}
		global.GVA_LOG.Info(fmt.Sprintf("任务 %s 第%d次失败，重置为pending状态准备重试", task.ID, newErrorCount))
	}

	// 检查批量工作流状态
	wp.checkBatchWorkflowCompletion(task.BatchWorkflowID)
}

// checkBatchWorkflowCompletion 检查批量工作流是否完成
func (wp *WorkerPool) checkBatchWorkflowCompletion(batchWorkflowID string) {
	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", batchWorkflowID).First(
		&batchWorkflow).Error; err != nil {
		return
	}

	// 统计任务状态
	var pendingCount, queuedCount, runningCount, completedCount, failedCount int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", batchWorkflowID, gaia.BatchTaskStatusPending).Count(&pendingCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", batchWorkflowID, gaia.BatchTaskStatusQueued).Count(&queuedCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", batchWorkflowID, gaia.BatchTaskStatusRunning).Count(&runningCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", batchWorkflowID, gaia.BatchTaskStatusCompleted).Count(&completedCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", batchWorkflowID, gaia.BatchTaskStatusFailed).Count(&failedCount)

	// 如果所有任务都已完成
	if completedCount == int64(batchWorkflow.TotalRows) {
		// 重置错误计数并更新状态，同时同步 processed_rows 确保数据一致性
		if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where("id = ?", batchWorkflowID).Updates(map[string]interface{}{
			"status":         gaia.BatchWorkflowStatusCompleted,
			"processed_rows": completedCount, // 同步已处理行数，确保数据一致性
			"error":          "",             // 清空错误信息
			"error_count":    0,              // 重置错误计数，恢复用户并发位
			"updated_at":     time.Now(),
		}).Error; err != nil {
			global.GVA_LOG.Error(fmt.Sprintf("更新批量工作流完成状态失败: %s", err.Error()))
		} else {
			global.GVA_LOG.Info(fmt.Sprintf("批量工作流 %s 已完成，错误计数已重置，用户 %d 的并发位将恢复", batchWorkflowID, batchWorkflow.UserID))
		}
	} else if pendingCount == 0 && queuedCount == 0 && runningCount == 0 && failedCount > 0 {
		// 如果没有待处理、排队或运行中的任务，但有失败的任务
		// 获取第一个失败任务的错误信息作为代表
		var failedTask gaia.BatchWorkflowTask
		var errorInfo = gaia.ErrorWorkflowFailed
		if err := global.GVA_DB.Where("batch_workflow_id = ? AND status = ?", batchWorkflowID,
			gaia.BatchTaskStatusFailed).First(&failedTask).Error; err == nil && failedTask.Error != "" {
			errorInfo = failedTask.Error
		}

		global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
			"id = ?", batchWorkflowID).Updates(map[string]interface{}{
			"status":     gaia.BatchWorkflowStatusFailed,
			"error":      errorInfo,
			"updated_at": time.Now(),
		})
	}
}

// resetAbnormalTasks 重置异常状态的任务
func resetAbnormalTasks() {
	if global.GVA_DB == nil {
		global.GVA_LOG.Error("数据库连接为空，无法重置异常任务状态")
		return
	}

	global.GVA_LOG.Info("开始重置异常状态的任务...")

	// 首先清理已停止的批量工作流中的待处理和排队任务
	cleanupStoppedBatchWorkflowTasks()

	// 重置 running 状态的任务为 pending
	runningResult := global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).
		Where("status = ?", gaia.BatchTaskStatusRunning).
		Update("status", gaia.BatchTaskStatusPending)

	if runningResult.Error != nil {
		global.GVA_LOG.Error("重置running状态任务失败: " + runningResult.Error.Error())
	} else if runningResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("重置了 %d 个running状态的任务为pending", runningResult.RowsAffected))
	}

	// 重置 queued 状态的任务为 pending
	queuedResult := global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).
		Where("status = ?", gaia.BatchTaskStatusQueued).
		Update("status", gaia.BatchTaskStatusPending)

	if queuedResult.Error != nil {
		global.GVA_LOG.Error("重置queued状态任务失败: " + queuedResult.Error.Error())
	} else if queuedResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("重置了 %d 个queued状态的任务为pending", queuedResult.RowsAffected))
	}

	// 重置相关批量工作流的状态
	// 如果批量工作流状态为 processing 但没有 running 或 queued 的任务，将其重置为 pending
	var batchWorkflows []gaia.BatchWorkflow
	err := global.GVA_DB.Where("status = ?", gaia.BatchWorkflowStatusProcessing).Find(&batchWorkflows).Error
	if err != nil {
		global.GVA_LOG.Error("查询processing状态的批量工作流失败: " + err.Error())
		return
	}

	for _, bw := range batchWorkflows {
		var runningCount int64
		global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).
			Where("batch_workflow_id = ? AND status IN (?)", bw.ID, []string{
				gaia.BatchTaskStatusRunning, gaia.BatchTaskStatusQueued}).Count(&runningCount)

		// 如果没有正在运行或排队的任务，将批量工作流状态重置为 pending
		if runningCount == 0 {
			if err = global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where("id = ?", bw.ID).Update(
				"status", gaia.BatchWorkflowStatusPending).Error; err != nil {
				global.GVA_LOG.Error(fmt.Sprintf("重置批量工作流 %s 状态失败: %s", bw.ID, err.Error()))
			}
		}
	}
}

// cleanupStoppedBatchWorkflowTasks 清理已停止的批量工作流中的待处理和排队任务
func cleanupStoppedBatchWorkflowTasks() {

	// 将已停止的批量工作流中的pending和queued任务标记为cancelled
	// 使用子查询方式避免JOIN在UPDATE中的别名问题
	result := global.GVA_DB.Table("batch_workflow_tasks_extend").Where(
		"batch_workflow_id IN (?) AND status IN (?)", global.GVA_DB.Table("batch_workflows_extend").Select(
			"id").Where("status = ?", gaia.BatchWorkflowStatusStopped), []string{
			gaia.BatchTaskStatusPending, gaia.BatchTaskStatusQueued}).Update(
		"status", gaia.BatchTaskStatusCancelled)

	if result.Error != nil {
		global.GVA_LOG.Error("清理已停止的批量工作流任务失败: " + result.Error.Error())
		return
	}
}

// InitWorkerPool 初始化全局工作池
func InitWorkerPool(workers int) {
	if globalWorkerPool != nil {
		globalWorkerPool.Stop()
	}

	// 重置所有异常状态的任务
	resetAbnormalTasks()

	globalWorkerPool = NewWorkerPool(workers)
	globalWorkerPool.Start()
}

// GetWorkerPool 获取全局工作池
func GetWorkerPool() *WorkerPool {
	return globalWorkerPool
}

// StopWorkerPool 停止全局工作池
func StopWorkerPool() {
	if globalWorkerPool != nil {
		globalWorkerPool.Stop()
		globalWorkerPool = nil
	}
}

// ResetBatchWorkflowErrorCount 重置指定批量工作流的错误计数
func ResetBatchWorkflowErrorCount(batchWorkflowID string) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	// 获取批量工作流信息
	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", batchWorkflowID).First(&batchWorkflow).Error; err != nil {
		return fmt.Errorf("批量工作流不存在: %v", err)
	}

	// 重置错误计数
	if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where("id = ?", batchWorkflowID).Updates(map[string]interface{}{
		"error_count": 0,
		"updated_at":  time.Now(),
	}).Error; err != nil {
		return fmt.Errorf("重置错误计数失败: %v", err)
	}

	global.GVA_LOG.Info(fmt.Sprintf("批量工作流 %s 的错误计数已手动重置，用户 %d 的并发位将恢复", batchWorkflowID, batchWorkflow.UserID))
	return nil
}

// ResetUserErrorCount 重置指定用户所有批量工作流的错误计数
func ResetUserErrorCount(userID uint) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	// 重置该用户所有批量工作流的错误计数
	result := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where("user_id = ?", userID).Updates(map[string]interface{}{
		"error_count": 0,
		"updated_at":  time.Now(),
	})

	if result.Error != nil {
		return fmt.Errorf("重置用户错误计数失败: %v", result.Error)
	}

	global.GVA_LOG.Info(fmt.Sprintf("用户 %d 的所有批量工作流错误计数已重置，影响 %d 个工作流，并发位将恢复", userID, result.RowsAffected))
	return nil
}

// handleInsufficientBalance 处理余额不足的情况，将用户所有pending和processing状态的工作流和任务设置为失败
// 特别处理同batch_workflow_id的所有任务
func (wp *WorkerPool) handleInsufficientBalance(userID uint, currentBatchWorkflowID string) {
	if global.GVA_DB == nil {
		global.GVA_LOG.Error("数据库连接未初始化，无法处理余额不足情况")
		return
	}

	// 优先处理当前batch_workflow_id的所有任务（包括processing状态）
	currentWorkflowResult := global.GVA_DB.Model(&gaia.BatchWorkflow{}).
		Where("id = ? AND status IN (?)", currentBatchWorkflowID, []string{gaia.BatchWorkflowStatusPending, gaia.BatchWorkflowStatusProcessing}).
		Updates(map[string]interface{}{
			"status":     gaia.BatchWorkflowStatusFailed,
			"error":      gaia.ErrorInsufficientBalance,
			"updated_at": time.Now(),
		})

	if currentWorkflowResult.Error != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新批量工作流 %s 状态失败: %s", currentBatchWorkflowID, currentWorkflowResult.Error.Error()))
	} else if currentWorkflowResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("已将批量工作流 %s 设置为失败状态", currentBatchWorkflowID))
	}

	// 将当前batch_workflow_id的所有未完成任务设置为失败
	currentTaskResult := global.GVA_DB.Table("batch_workflow_tasks_extend").
		Where("batch_workflow_id = ? AND status IN (?)", currentBatchWorkflowID, []string{gaia.BatchTaskStatusPending, gaia.BatchTaskStatusQueued, gaia.BatchTaskStatusRunning}).
		Updates(map[string]interface{}{
			"status":     gaia.BatchTaskStatusFailed,
			"error":      gaia.ErrorInsufficientBalance,
			"updated_at": time.Now(),
		})

	if currentTaskResult.Error != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新批量工作流 %s 的任务状态失败: %s", currentBatchWorkflowID, currentTaskResult.Error.Error()))
	} else if currentTaskResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("已将批量工作流 %s 的 %d 个任务设置为失败状态", currentBatchWorkflowID, currentTaskResult.RowsAffected))
	}

	// 将用户其他所有pending状态的批量工作流设置为失败
	otherWorkflowResult := global.GVA_DB.Model(&gaia.BatchWorkflow{}).
		Where("user_id = ? AND id != ? AND status = ?", userID, currentBatchWorkflowID, gaia.BatchWorkflowStatusPending).
		Updates(map[string]interface{}{
			"status":     gaia.BatchWorkflowStatusFailed,
			"error":      gaia.ErrorInsufficientBalance,
			"updated_at": time.Now(),
		})

	if otherWorkflowResult.Error != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新用户 %d 其他pending批量工作流状态失败: %s", userID, otherWorkflowResult.Error.Error()))
	} else if otherWorkflowResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("已将用户 %d 的 %d 个其他pending批量工作流设置为失败状态", userID, otherWorkflowResult.RowsAffected))
	}

	// 将用户其他所有pending状态的批量工作流任务设置为失败
	otherTaskResult := global.GVA_DB.Table("batch_workflow_tasks_extend").
		Where("batch_workflow_id IN (?) AND batch_workflow_id != ? AND status = ?",
			global.GVA_DB.Table("batch_workflows_extend").Select("id").Where("user_id = ?", userID),
			currentBatchWorkflowID,
			gaia.BatchTaskStatusPending).
		Updates(map[string]interface{}{
			"status":     gaia.BatchTaskStatusFailed,
			"error":      gaia.ErrorInsufficientBalance,
			"updated_at": time.Now(),
		})

	if otherTaskResult.Error != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新用户 %d 其他pending批量工作流任务状态失败: %s", userID, otherTaskResult.Error.Error()))
	} else if otherTaskResult.RowsAffected > 0 {
		global.GVA_LOG.Info(fmt.Sprintf("已将用户 %d 的 %d 个其他pending批量工作流任务设置为失败状态", userID, otherTaskResult.RowsAffected))
	}
}

// GetBatchWorkflowList 获取最近30天的批量工作流列表
func (s *BatchWorkflowService) GetBatchWorkflowList(userID uint, installedID string, page, limit int) ([]gaia.BatchWorkflow, int64, error) {
	if global.GVA_DB == nil {
		return nil, 0, fmt.Errorf("数据库连接未初始化")
	}

	// 计算30天前的时间
	thirtyDaysAgo := time.Now().AddDate(0, 0, -30)

	// 构建查询条件
	query := global.GVA_DB.Model(&gaia.BatchWorkflow{}).
		Where("user_id = ? AND created_at >= ?", userID, thirtyDaysAgo)

	// 如果指定了installedID，则添加该条件
	if installedID != "" {
		query = query.Where("installed_id = ?", installedID)
	}

	// 获取总数
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}

	// 分页查询
	var batchWorkflows []gaia.BatchWorkflow
	offset := (page - 1) * limit
	if err := query.Order("created_at DESC").
		Limit(limit).
		Offset(offset).
		Find(&batchWorkflows).Error; err != nil {
		return nil, 0, err
	}

	// 解码错误信息中的 Unicode 转义序列
	for i := range batchWorkflows {
		if batchWorkflows[i].Error != "" {
			batchWorkflows[i].Error = decodeUnicodeEscapes(batchWorkflows[i].Error)
		}
	}

	return batchWorkflows, total, nil
}
