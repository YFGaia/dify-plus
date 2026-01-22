package gaia

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/pkg/errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/google/uuid"
)

type BatchWorkflowService struct{}

// CreateBatchWorkflow 创建批量处理工作流

func (s *BatchWorkflowService) CreateBatchWorkflow(
	userId uint, installedID, fileName string, fileContent [][]string, keyNameMapping map[string]string) (
	*gaia.BatchWorkflow, error) {
	// 检查数据库连接
	if global.GVA_DB == nil {
		return nil, fmt.Errorf("数据库连接未初始化")
	}

	// 创建批量处理记录
	keyByte, _ := json.Marshal(keyNameMapping)
	batchWorkflow := &gaia.BatchWorkflow{
		ProcessedRows: 0,
		UserID:        userId,
		FileName:      fileName,
		Status:        "pending",
		CreatedAt:     time.Now(),
		UpdatedAt:     time.Now(),
		InstalledID:   installedID,
		KeyName:       string(keyByte),
		ID:            uuid.New().String(),
		TotalRows:     0, // 先设为0，后面会更新为实际有效行数
	}

	// 保存到数据库
	if err := global.GVA_DB.Create(batchWorkflow).Error; err != nil {
		return nil, fmt.Errorf("保存批量处理记录失败: %v", err)
	}

	// 创建任务记录
	headers := fileContent[0]
	if len(headers) > 0 {
		// 去除UTF-8 BOM
		headers[0] = strings.TrimPrefix(headers[0], "\uFEFF")
	}
	dataRows := fileContent[1:]

	validRowCount := 0 // 记录有效行数
	for i, row := range dataRows {
		// 构建输入参数
		inputs := make(map[string]string)
		hasNonEmptyValue := false // 检查是否有非空值

		for j, value := range row {
			if j < len(headers) {
				headerName := headers[j]
				// 去除首尾空格
				value = strings.TrimSpace(value)

				// 如果有key-name映射，使用映射后的key，否则使用原始header
				if keyNameMapping != nil {
					if key, exists := keyNameMapping[headerName]; exists {
						inputs[key] = value
					} else {
						inputs[headerName] = value
					}
				} else {
					inputs[headerName] = value
				}

				// 检查是否有非空值
				if value != "" {
					hasNonEmptyValue = true
				}
			}
		}

		// 如果所有字段都为空，跳过这一行
		if !hasNonEmptyValue {
			global.GVA_LOG.Info(fmt.Sprintf("跳过空值行，行索引: %d", i+1))
			continue
		}

		validRowCount++
		inputsJSON, _ := json.Marshal(inputs)

		task := &gaia.BatchWorkflowTask{
			ID:              uuid.New().String(),
			BatchWorkflowID: batchWorkflow.ID,
			RowIndex:        i + 1,
			Inputs:          string(inputsJSON),
			Status:          "pending",
			CreatedAt:       time.Now(),
			UpdatedAt:       time.Now(),
		}

		if err := global.GVA_DB.Create(task).Error; err != nil {
			return nil, fmt.Errorf("创建任务记录失败: %v", err)
		}
	}

	// 更新批量处理记录的总行数为实际有效行数
	if err := global.GVA_DB.Model(batchWorkflow).Update("total_rows", validRowCount).Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新总行数失败: %v", err))
	}

	// 任务已创建，工作池会自动处理
	// 确保工作池在运行
	if pool := GetWorkerPool(); pool == nil || !pool.IsRunning() {
		global.GVA_LOG.Warn("工作池未运行，尝试重新启动")
		InitWorkerPool(global.GVA_CONFIG.System.WorkFlowNumber) // 默认5个worker
	}

	// 更新批处理工作流状态为处理中
	if err := global.GVA_DB.Model(batchWorkflow).Update("status", "processing").Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("更新批处理工作流状态失败: %v", err))
	}

	return batchWorkflow, nil
}

// parseSSEStream 解析SSE流并返回最终结果
func (s *BatchWorkflowService) parseSSEStream(body []byte) (*request.WorkflowResult, error) {
	lines := strings.Split(string(body), "\n")
	result := &request.WorkflowResult{
		Nodes: make([]request.NodeExecution, 0),
	}
	nodeMap := make(map[string]*request.NodeExecution) // 用于跟踪节点状态

	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" || !strings.HasPrefix(line, "data: ") {
			continue
		}

		// 移除 "data: " 前缀
		jsonStr := strings.TrimPrefix(line, "data: ")

		// 解析JSON
		var event map[string]interface{}
		if err := json.Unmarshal([]byte(jsonStr), &event); err != nil {
			continue // 跳过无法解析的行
		}

		eventType, ok := event["event"].(string)
		if !ok {
			continue
		}

		// 兼容新旧格式：如果有data字段则使用data，否则使用顶层数据
		var data map[string]interface{}
		if dataField, hasData := event["data"].(map[string]interface{}); hasData {
			// 旧格式：事件数据在data字段中
			data = dataField
		} else {
			// 新格式：事件数据在顶层
			data = event
		}

		switch eventType {
		case "workflow_started":
			if workflowRunID, ok := data["id"].(string); ok {
				result.WorkflowRunID = workflowRunID
			}
			if workflowID, ok := data["workflow_id"].(string); ok {
				result.WorkflowID = workflowID
			}
			if sequenceNumber, ok := data["sequence_number"].(float64); ok {
				result.SequenceNumber = int(sequenceNumber)
			}
			if createdAt, ok := data["created_at"].(float64); ok {
				result.CreatedAt = int64(createdAt)
			}

		case "node_started":
			nodeExecution := &request.NodeExecution{}
			if id, ok := data["id"].(string); ok {
				nodeExecution.ID = id
			}
			if nodeID, ok := data["node_id"].(string); ok {
				nodeExecution.NodeID = nodeID
			}
			if nodeType, ok := data["node_type"].(string); ok {
				nodeExecution.NodeType = nodeType
			}
			if title, ok := data["title"].(string); ok {
				nodeExecution.Title = title
			}
			if index, ok := data["index"].(float64); ok {
				nodeExecution.Index = int(index)
			}
			if inputs, ok := data["inputs"].(map[string]interface{}); ok {
				nodeExecution.Inputs = inputs
			}
			if createdAt, ok := data["created_at"].(float64); ok {
				nodeExecution.CreatedAt = int64(createdAt)
			}

			nodeMap[nodeExecution.ID] = nodeExecution

		case "node_finished":
			nodeID, ok := data["id"].(string)
			if !ok {
				continue
			}

			node, exists := nodeMap[nodeID]
			if !exists {
				// 如果没有找到对应的开始节点，创建一个新的
				node = &request.NodeExecution{}
				if id, ok := data["id"].(string); ok {
					node.ID = id
				}
				if nodeIDStr, ok := data["node_id"].(string); ok {
					node.NodeID = nodeIDStr
				}
				if nodeType, ok := data["node_type"].(string); ok {
					node.NodeType = nodeType
				}
				if title, ok := data["title"].(string); ok {
					node.Title = title
				}
				if index, ok := data["index"].(float64); ok {
					node.Index = int(index)
				}
				nodeMap[nodeID] = node
			}

			// 更新节点完成信息
			if status, ok := data["status"].(string); ok {
				node.Status = status
			}
			if errorMsg, ok := data["error"].(string); ok && errorMsg != "" {
				node.Error = errorMsg
			}
			if elapsedTime, ok := data["elapsed_time"].(float64); ok {
				node.ElapsedTime = elapsedTime
			}
			if outputs, ok := data["outputs"].(map[string]interface{}); ok {
				node.Outputs = outputs
			}
			if finishedAt, ok := data["finished_at"].(float64); ok {
				node.FinishedAt = int64(finishedAt)
			}

		case "workflow_finished":
			if status, ok := data["status"].(string); ok {
				result.Status = status
			}
			if outputs, ok := data["outputs"].(map[string]interface{}); ok {
				result.Outputs = outputs
			}
			if errorMsg, ok := data["error"].(string); ok {
				result.Error = errorMsg
			}
			if elapsedTime, ok := data["elapsed_time"].(float64); ok {
				result.ElapsedTime = elapsedTime
			}
			if totalTokens, ok := data["total_tokens"].(float64); ok {
				result.TotalTokens = int(totalTokens)
			}
			if totalSteps, ok := data["total_steps"].(float64); ok {
				result.TotalSteps = int(totalSteps)
			}
			if exceptionsCount, ok := data["exceptions_count"].(float64); ok {
				result.ExceptionsCount = int(exceptionsCount)
			}
			if finishedAt, ok := data["finished_at"].(float64); ok {
				result.FinishedAt = int64(finishedAt)
			}

		case "message":
			// 处理新的message事件格式，将answer字段填充到outputs.text中
			if answer, ok := data["answer"].(string); ok && answer != "" {
				// 如果result.Outputs为空，初始化它
				if result.Outputs == nil {
					result.Outputs = make(map[string]interface{})
				}
				if value, okText := result.Outputs["text"]; okText {
					result.Outputs["text"] = value.(string) + answer
				} else {
					result.Outputs["text"] = answer
				}
			}
			// 同时设置其他相关字段
			if messageID, ok := data["message_id"].(string); ok {
				result.WorkflowRunID = messageID
			}
			if createdAt, ok := data["created_at"].(float64); ok {
				result.CreatedAt = int64(createdAt)
			}
		}
	}

	// 将节点按照index排序并添加到结果中
	for _, node := range nodeMap {
		result.Nodes = append(result.Nodes, *node)
	}

	// 按index排序
	for i := 0; i < len(result.Nodes)-1; i++ {
		for j := i + 1; j < len(result.Nodes); j++ {
			if result.Nodes[i].Index > result.Nodes[j].Index {
				result.Nodes[i], result.Nodes[j] = result.Nodes[j], result.Nodes[i]
			}
		}
	}

	return result, nil
}

// callDifyAPI 调用Dify API
func (s *BatchWorkflowService) callDifyAPI(
	installedID, userToken, csrfToken string, inputs map[string]string) (string, error) {

	var err error
	var requestBodyJSON []byte
	if requestBodyJSON, err = json.Marshal(&map[string]interface{}{
		"inputs":        inputs,
		"response_mode": "streaming",
	}); err != nil {
		return "", err
	}
	var url string
	var mode sql.NullString
	if err = global.GVA_DB.Raw("SELECT b.mode FROM installed_apps as a, apps as b WHERE a.app_id=b.id AND a.id = ?", installedID).Scan(&mode).Error; err != nil {
		return "", err
	}
	// 区分model
	if mode.String == "workflow" {
		url = "%s/console/api/installed-apps/%s/workflows/run"
	} else if mode.String == "completion" {
		url = "%s/console/api/installed-apps/%s/completion-messages"
	} else {
		return "", errors.New(fmt.Sprintf("Unsupported dify API call: %s", mode.String))
	}
	// 创建HTTP请求
	req, err := http.NewRequest("POST", fmt.Sprintf(
		url, global.GVA_CONFIG.Gaia.Url, installedID), strings.NewReader(string(requestBodyJSON)))
	if err != nil {
		return "", err
	}

	// 设置请求头
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+userToken)
	req.Header.Set("Accept", "text/event-stream")
	// Extend Start: 添加CSRF token支持
	if csrfToken != "" {
		req.Header.Set("x-csrf-token", csrfToken)
		req.Header.Set("Cookie", fmt.Sprintf("csrf_token=%s", csrfToken))
	}
	// Extend End: 添加CSRF token支持

	// 发送请求
	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	// 读取响应
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("API请求失败，状态码: %d, 响应: %s", resp.StatusCode, string(body))
	}

	// 解析SSE流
	result, err := s.parseSSEStream(body)
	if err != nil {
		return "", fmt.Errorf("解析SSE流失败: %v", err)
	}

	// 将结果转换为JSON字符串返回
	resultJSON, err := json.Marshal(result)
	if err != nil {
		return "", fmt.Errorf("序列化结果失败: %v", err)
	}

	return string(resultJSON), nil
}

// GetBatchWorkflow 获取批量处理信息
func (s *BatchWorkflowService) GetBatchWorkflow(id string) (*gaia.BatchWorkflow, error) {
	if global.GVA_DB == nil {
		return nil, fmt.Errorf("数据库连接未初始化")
	}

	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", id).First(
		&batchWorkflow).Error; err != nil {
		return nil, err
	}
	return &batchWorkflow, nil
}

// GetBatchWorkflowTasks 获取批量处理的任务列表
func (s *BatchWorkflowService) GetBatchWorkflowTasks(
	batchWorkflowID string) ([]gaia.BatchWorkflowTask, error) {
	if global.GVA_DB == nil {
		return nil, fmt.Errorf("数据库连接未初始化")
	}

	var tasks []gaia.BatchWorkflowTask
	if err := global.GVA_DB.Where("batch_workflow_id = ?", batchWorkflowID).Order(
		"row_index").Find(&tasks).Error; err != nil {
		return nil, err
	}
	return tasks, nil
}

// StopBatchWorkflow 停止批量处理
func (s *BatchWorkflowService) StopBatchWorkflow(id string) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	return global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where("id = ?", id).Update(
		"status", "stopped").Error
}

// RetryFailedTasks 仅重试失败的任务
func (s *BatchWorkflowService) RetryFailedTasks(id string) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	// 只重置失败的任务为待处理状态，保留已完成的任务
	errorCount := 0
	taskList := []string{"failed", "queued", "running"}
	if err := global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status IN ?", id, taskList).Updates(map[string]interface{}{
		"status":      "pending",
		"error":       "",
		"error_count": &errorCount,
		"updated_at":  time.Now(),
	}).Error; err != nil {
		return err
	}

	// 重新计算已处理行数
	var completedCount int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", id, "completed").Count(&completedCount)

	// 重置批量处理状态
	if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
		"id = ?", id).Updates(map[string]interface{}{
		"status":         "pending",
		"processed_rows": completedCount,
		"error":          "",
		"updated_at":     time.Now(),
	}).Error; err != nil {
		return err
	}

	global.GVA_LOG.Info(fmt.Sprintf("批量工作流 %s 失败任务重试已启动，工作池将自动处理待处理任务", id))
	return nil
}

// RetryBatchWorkflow 重试批量处理
func (s *BatchWorkflowService) RetryBatchWorkflow(id string) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	// 重置所有失败的任务为待处理状态
	if err := global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status IN ?", id, []string{"failed", "queued", "running"}).Updates(map[string]interface{}{
		"status":     "pending",
		"error":      "",
		"updated_at": time.Now(),
	}).Error; err != nil {
		return err
	}

	// 重新计算已处理行数
	var completedCount int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", id, "completed").Count(&completedCount)

	// 重置批量处理状态
	if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
		"id = ?", id).Updates(map[string]interface{}{
		"status":         "processing",
		"processed_rows": completedCount,
		"error":          "",
		"updated_at":     time.Now(),
	}).Error; err != nil {
		return err
	}

	// 确保工作池在运行
	if pool := GetWorkerPool(); pool == nil || !pool.IsRunning() {
		global.GVA_LOG.Warn("工作池未运行，尝试重新启动")
		InitWorkerPool(global.GVA_CONFIG.System.WorkFlowNumber)
	}

	global.GVA_LOG.Info(fmt.Sprintf("批量工作流 %s 重试已启动，工作池将自动处理待处理任务", id))
	return nil
}

// ResumeBatchWorkflow 恢复批量处理
func (s *BatchWorkflowService) ResumeBatchWorkflow(id string) error {
	if global.GVA_DB == nil {
		return fmt.Errorf("数据库连接未初始化")
	}

	// 检查批量工作流是否存在
	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", id).First(&batchWorkflow).Error; err != nil {
		return fmt.Errorf("批量工作流不存在: %v", err)
	}

	// 检查批量工作流状态是否为stopped
	if batchWorkflow.Status != "stopped" {
		return fmt.Errorf("只能恢复已停止的批量处理")
	}

	// 重新计算已处理行数（基于实际完成的任务数）
	var completedCount int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status = ?", id, "completed").Count(&completedCount)

	// 同步 processed_rows 字段
	if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
		"id = ?", id).Update("processed_rows", completedCount).Error; err != nil {
		global.GVA_LOG.Error(fmt.Sprintf("同步已处理行数失败: %v", err))
	}

	// 检查是否所有任务都已完成
	if completedCount == int64(batchWorkflow.TotalRows) {
		// 所有任务都已完成，更新状态为 completed
		if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
			"id = ?", id).Updates(map[string]interface{}{
			"status":         "completed",
			"processed_rows": completedCount,
			"error":          "",
			"error_count":    0,
			"updated_at":     time.Now(),
		}).Error; err != nil {
			return fmt.Errorf("更新批量工作流完成状态失败: %v", err)
		}
		return nil
	}

	// 检查是否有可恢复的任务（pending 或 cancelled 状态）
	var resumableTasks int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
		"batch_workflow_id = ? AND status IN (?)", id, []string{"pending", "cancelled"}).Count(&resumableTasks)

	if resumableTasks == 0 {
		// 没有可恢复的任务，但也没有全部完成，可能是数据不一致
		// 检查是否有其他状态的任务
		var otherTasks int64
		global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where(
			"batch_workflow_id = ? AND status NOT IN (?)", id, []string{"completed", "failed"}).Count(&otherTasks)

		if otherTasks == 0 {
			// 所有任务都是 completed 或 failed，但 completedCount 不等于 TotalRows
			// 可能是数据不一致，尝试调用 checkBatchWorkflowCompletion 来修复
			if pool := GetWorkerPool(); pool != nil {
				pool.checkBatchWorkflowCompletion(id)
			}
			return fmt.Errorf("没有可恢复的任务，但任务状态可能不一致，已尝试修复")
		}
		return fmt.Errorf("没有可恢复的任务")
	}

	// 将cancelled状态的任务恢复为pending状态
	if err := global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).
		Where("batch_workflow_id = ? AND status IN (?)", id, []string{"pending", "cancelled"}).
		Updates(map[string]interface{}{
			"status":     "pending",
			"updated_at": time.Now(),
		}).Error; err != nil {
		return fmt.Errorf("恢复取消的任务失败: %v", err)
	}

	// 更新批量工作流状态为处理中
	if err := global.GVA_DB.Model(&gaia.BatchWorkflow{}).Where(
		"id = ?", id).Updates(map[string]interface{}{
		"status":         "processing",
		"processed_rows": completedCount,
		"updated_at":     time.Now(),
	}).Error; err != nil {
		return err
	}

	// 确保工作池在运行
	if pool := GetWorkerPool(); pool == nil || !pool.IsRunning() {
		global.GVA_LOG.Warn("工作池未运行，尝试重新启动")
		InitWorkerPool(global.GVA_CONFIG.System.WorkFlowNumber)
	}

	return nil
}

// GetBatchWorkflowProgress 获取批量处理进度
func (s *BatchWorkflowService) GetBatchWorkflowProgress(id string) (map[string]interface{}, error) {
	if global.GVA_DB == nil {
		return nil, fmt.Errorf("数据库连接未初始化")
	}

	var batchWorkflow gaia.BatchWorkflow
	if err := global.GVA_DB.Where("id = ?", id).First(&batchWorkflow).Error; err != nil {
		return nil, err
	}

	// 统计各种状态的任务数量
	var pendingCount, queuedCount, runningCount, completedCount, failedCount int64
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where("batch_workflow_id = ? AND status = ?", id, "pending").Count(&pendingCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where("batch_workflow_id = ? AND status = ?", id, "queued").Count(&queuedCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where("batch_workflow_id = ? AND status = ?", id, "running").Count(&runningCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where("batch_workflow_id = ? AND status = ?", id, "completed").Count(&completedCount)
	global.GVA_DB.Model(&gaia.BatchWorkflowTask{}).Where("batch_workflow_id = ? AND status = ?", id, "failed").Count(&failedCount)

	progress := float64(completedCount) / float64(batchWorkflow.TotalRows) * 100

	// 获取工作池状态
	var workerPoolStatus map[string]interface{}
	if pool := GetWorkerPool(); pool != nil {
		workerPoolStatus = pool.GetStatus()
	} else {
		workerPoolStatus = map[string]interface{}{
			"running":      false,
			"workers":      0,
			"queue_length": 0,
		}
	}

	// 获取错误信息 - 从批量工作流本身和失败的任务中获取
	var errorInfo string
	if batchWorkflow.Error != "" {
		errorInfo = batchWorkflow.Error
	} else if failedCount > 0 {
		// 如果有失败的任务，获取第一个失败任务的错误信息作为代表
		var failedTask gaia.BatchWorkflowTask
		if err := global.GVA_DB.Where("batch_workflow_id = ? AND status = ?", id, "failed").First(&failedTask).Error; err == nil && failedTask.Error != "" {
			errorInfo = failedTask.Error
		}
	}

	return map[string]interface{}{
		"id":                 batchWorkflow.ID,
		"status":             batchWorkflow.Status,
		"total_rows":         batchWorkflow.TotalRows,
		"processed_rows":     completedCount, // 使用实时统计值确保与progress一致
		"progress":           progress,
		"pending_count":      pendingCount,
		"queued_count":       queuedCount,
		"running_count":      runningCount,
		"completed_count":    completedCount,
		"failed_count":       failedCount,
		"error":              errorInfo, // 添加错误信息
		"worker_pool_status": workerPoolStatus,
		"created_at":         batchWorkflow.CreatedAt,
		"updated_at":         batchWorkflow.UpdatedAt,
	}, nil
}

// GetWorkerPool 获取全局工作池
func (s *BatchWorkflowService) GetWorkerPool() *WorkerPool {
	return GetWorkerPool()
}

// InitWorkerPool 初始化工作池
func (s *BatchWorkflowService) InitWorkerPool(workers int) {
	InitWorkerPool(workers)
}

// StopWorkerPool 停止工作池
func (s *BatchWorkflowService) StopWorkerPool() {
	StopWorkerPool()
}
