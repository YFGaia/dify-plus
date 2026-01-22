package gaia

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"golang.org/x/text/encoding/simplifiedchinese"
	"golang.org/x/text/transform"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	"github.com/flipped-aurora/gin-vue-admin/server/service"
	gaiaService "github.com/flipped-aurora/gin-vue-admin/server/service/gaia"
	"github.com/gin-gonic/gin"
)

type BatchWorkflowApi struct{}

var batchWorkflowService = service.ServiceGroupApp.GaiaServiceGroup.BatchWorkflowService

// CreateBatchWorkflow 创建批量处理工作流
// @Tags BatchWorkflow
// @Summary 创建批量处理工作流
// @Description 上传CSV文件并创建批量处理工作流
// @Accept multipart/form-data
// @Produce application/json
// @Param file formData file true "CSV文件"
// @Param installed_id formData string true "安装的应用ID"
// @Param app_id formData string true "应用ID"
// @Param tenant_id formData string true "租户ID"
// @Success 200 {object} response.Response{data=gaia.BatchWorkflow} "成功"
// @Router /gaia/workflow/batch/processing [post]
func (api *BatchWorkflowApi) CreateBatchWorkflow(c *gin.Context) {
	// 获取表单参数
	userID := utils.GetUserID(c)
	installedID := c.PostForm("installed_id")
	keyNameMappingStr := c.PostForm("key_name_mapping")

	if installedID == "" {
		response.FailWithMessage("缺少必要参数", c)
		return
	}

	// 解析key-name映射
	var keyNameMapping map[string]string
	if keyNameMappingStr != "" {
		if err := json.Unmarshal([]byte(keyNameMappingStr), &keyNameMapping); err != nil {
			response.FailWithMessage("解析key_name_mapping失败: "+err.Error(), c)
			return
		}
	}

	// 获取上传的文件
	file, err := c.FormFile("file")
	if err != nil {
		response.FailWithMessage("获取文件失败: "+err.Error(), c)
		return
	}

	// 打开上传的文件
	src, err := file.Open()
	if err != nil {
		response.FailWithMessage("打开文件失败: "+err.Error(), c)
		return
	}
	defer src.Close()

	// 读取文件内容并检测编码
	content, err := io.ReadAll(src)
	if err != nil {
		response.FailWithMessage("读取文件内容失败: "+err.Error(), c)
		return
	}

	// 尝试不同编码解析CSV
	var data [][]string
	var parseErr error

	// 1. 先尝试UTF-8读取，使用宽松的CSV解析器配置
	reader := bytes.NewReader(content)
	csvReader := csv.NewReader(reader)
	csvReader.LazyQuotes = true       // 允许懒惰引号
	csvReader.TrimLeadingSpace = true // 去除前导空格
	data, parseErr = csvReader.ReadAll()

	// 2. 如果UTF-8失败或包含乱码，尝试GBK编码
	if parseErr != nil || containsGarbledText(data) {
		decoder := simplifiedchinese.GBK.NewDecoder()
		gbkReader := transform.NewReader(bytes.NewReader(content), decoder)

		csvReader = csv.NewReader(gbkReader)
		csvReader.LazyQuotes = true       // 允许懒惰引号
		csvReader.TrimLeadingSpace = true // 去除前导空格
		data, parseErr = csvReader.ReadAll()

		// 3. 如果GBK也失败，尝试GB18030编码
		if parseErr != nil || containsGarbledText(data) {
			gb18030Decoder := simplifiedchinese.GB18030.NewDecoder()
			gb18030Reader := transform.NewReader(bytes.NewReader(content), gb18030Decoder)

			csvReader = csv.NewReader(gb18030Reader)
			csvReader.LazyQuotes = true       // 允许懒惰引号
			csvReader.TrimLeadingSpace = true // 去除前导空格
			data, parseErr = csvReader.ReadAll()
		}
	}

	// 4. 如果以上方法都失败，尝试最后的兜底解析方法
	if parseErr != nil {
		data, parseErr = parseCSVWithFallback(content)
	}

	if parseErr != nil {
		response.FailWithMessage("解析CSV文件失败，请检查文件格式。错误详情: "+parseErr.Error(), c)
		return
	}

	// 创建批量处理工作流
	batchWorkflow, err := batchWorkflowService.CreateBatchWorkflow(
		userID, installedID, file.Filename, data, keyNameMapping)
	if err != nil {
		// 特别处理数据库连接问题
		if strings.Contains(err.Error(), "数据库连接未初始化") {
			response.FailWithMessage("系统初始化中，请稍后重试", c)
		} else {
			response.FailWithMessage("创建批量处理失败: "+err.Error(), c)
		}
		return
	}

	response.OkWithData(batchWorkflow, c)
}

// GetBatchWorkflow 获取批量处理信息
// @Tags BatchWorkflow
// @Summary 获取批量处理信息
// @Description 根据ID获取批量处理信息
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response{data=gaia.BatchWorkflow} "成功"
// @Router /gaia/workflow/batch/{id} [get]
func (api *BatchWorkflowApi) GetBatchWorkflow(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	batchWorkflow, err := batchWorkflowService.GetBatchWorkflow(id)
	if err != nil {
		response.FailWithMessage("获取批量处理信息失败: "+err.Error(), c)
		return
	}

	response.OkWithData(batchWorkflow, c)
}

// GetBatchWorkflowTasks 获取批量处理任务列表
// @Tags BatchWorkflow
// @Summary 获取批量处理任务列表
// @Description 根据批量处理ID获取任务列表
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response{data=[]gaia.BatchWorkflowTask} "成功"
// @Router /gaia/workflow/batch/{id}/tasks [get]
func (api *BatchWorkflowApi) GetBatchWorkflowTasks(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	tasks, err := batchWorkflowService.GetBatchWorkflowTasks(id)
	if err != nil {
		response.FailWithMessage("获取任务列表失败: "+err.Error(), c)
		return
	}

	response.OkWithData(tasks, c)
}

// GetBatchWorkflowProgress 获取批量处理进度
// @Tags BatchWorkflow
// @Summary 获取批量处理进度
// @Description 根据ID获取批量处理进度信息
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response{data=map[string]interface{}} "成功"
// @Router /gaia/workflow/batch/{id}/progress [get]
func (api *BatchWorkflowApi) GetBatchWorkflowProgress(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	progress, err := batchWorkflowService.GetBatchWorkflowProgress(id)
	if err != nil {
		response.FailWithMessage("获取进度信息失败: "+err.Error(), c)
		return
	}

	response.OkWithData(progress, c)
}

// StopBatchWorkflow 停止批量处理
// @Tags BatchWorkflow
// @Summary 停止批量处理
// @Description 根据ID停止批量处理
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/{id}/stop [post]
func (api *BatchWorkflowApi) StopBatchWorkflow(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	err := batchWorkflowService.StopBatchWorkflow(id)
	if err != nil {
		response.FailWithMessage("停止批量处理失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("停止成功", c)
}

// RetryBatchWorkflow 重试批量处理（重新开始所有任务）
// @Tags BatchWorkflow
// @Summary 重试批量处理
// @Description 根据ID重试批量处理，重置所有任务从头开始
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/{id}/retry [post]
func (api *BatchWorkflowApi) RetryBatchWorkflow(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	err := batchWorkflowService.RetryBatchWorkflow(id)
	if err != nil {
		response.FailWithMessage("重试批量处理失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("重试成功，所有任务已重置", c)
}

// RetryFailedTasks 仅重试失败的任务
// @Tags BatchWorkflow
// @Summary 仅重试失败的任务
// @Description 根据ID仅重试失败的任务，保留已完成的任务
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/{id}/retry-failed [post]
func (api *BatchWorkflowApi) RetryFailedTasks(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	err := batchWorkflowService.RetryFailedTasks(id)
	if err != nil {
		response.FailWithMessage("重试失败任务失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("失败任务重试成功", c)
}

// ResumeBatchWorkflow 恢复批量处理
// @Tags BatchWorkflow
// @Summary 恢复批量处理
// @Description 根据ID恢复批量处理
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/{id}/resume [post]
func (api *BatchWorkflowApi) ResumeBatchWorkflow(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	err := batchWorkflowService.ResumeBatchWorkflow(id)
	if err != nil {
		response.FailWithMessage("恢复批量处理失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("恢复成功", c)
}

// ResetBatchWorkflowErrorCount 重置批量工作流错误计数
// @Tags BatchWorkflow
// @Summary 重置批量工作流错误计数
// @Description 重置指定批量工作流的错误计数，恢复用户并发位
// @Produce application/json
// @Param id path string true "批量处理ID"
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/{id}/reset-error-count [post]
func (api *BatchWorkflowApi) ResetBatchWorkflowErrorCount(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	// 调用worker_pool中的重置函数
	err := gaiaService.ResetBatchWorkflowErrorCount(id)
	if err != nil {
		response.FailWithMessage("重置错误计数失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("错误计数已重置，用户并发位将恢复", c)
}

// ResetUserErrorCount 重置用户所有批量工作流错误计数
// @Tags BatchWorkflow
// @Summary 重置用户所有批量工作流错误计数
// @Description 重置指定用户所有批量工作流的错误计数，恢复用户并发位
// @Produce application/json
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/batch/reset-user-error-count [post]
func (api *BatchWorkflowApi) ResetUserErrorCount(c *gin.Context) {
	userID := utils.GetUserID(c)

	// 调用worker_pool中的重置函数
	err := gaiaService.ResetUserErrorCount(userID)
	if err != nil {
		response.FailWithMessage("重置用户错误计数失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("用户所有批量工作流错误计数已重置，并发位将恢复", c)
}

// DownloadBatchWorkflowResults 下载批量处理结果
// @Tags BatchWorkflow
// @Summary 下载批量处理结果
// @Description 根据ID下载批量处理结果
// @Produce text/csv
// @Param id path string true "批量处理ID"
// @Success 200 {file} file "CSV文件"
// @Router /gaia/workflow/batch/{id}/download [get]
func (api *BatchWorkflowApi) DownloadBatchWorkflowResults(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		response.FailWithMessage("缺少批量处理ID", c)
		return
	}

	// 获取批量处理信息
	flow, err := batchWorkflowService.GetBatchWorkflow(id)
	if err != nil {
		response.FailWithMessage("获取批量处理信息失败: "+err.Error(), c)
		return
	}

	// 获取任务列表
	tasks, err := batchWorkflowService.GetBatchWorkflowTasks(id)
	if err != nil {
		response.FailWithMessage("获取任务列表失败: "+err.Error(), c)
		return
	}

	// 生成CSV内容
	csvContent := generateCSVFromTasks(flow, tasks)
	csvBytes := []byte(csvContent)

	// 添加 UTF-8 BOM 以确保在 Excel 中正确显示中文
	bom := []byte{0xEF, 0xBB, 0xBF}
	fullContent := append(bom, csvBytes...)

	// 设置响应头
	filename := fmt.Sprintf("batch_results_%s.csv", id)
	c.Header("Content-Type", "text/csv; charset=utf-8")
	c.Header("Content-Disposition", fmt.Sprintf("attachment; filename*=UTF-8''%s", filename))
	c.Header("Content-Length", fmt.Sprintf("%d", len(fullContent)))
	c.Header("Cache-Control", "no-cache, no-store, must-revalidate")
	c.Header("Pragma", "no-cache")
	c.Header("Expires", "0")

	c.Data(http.StatusOK, "text/csv; charset=utf-8", fullContent)
}

// parseCSVWithFallback 兜底的CSV解析方法，用于处理格式不规范的CSV文件
func parseCSVWithFallback(content []byte) ([][]string, error) {
	// 将内容转换为字符串并按行分割
	contentStr := string(content)
	lines := strings.Split(contentStr, "\n")

	var result [][]string
	for i, line := range lines {
		// 跳过空行
		if strings.TrimSpace(line) == "" {
			continue
		}

		// 尝试简单的逗号分割
		fields := strings.Split(line, ",")

		// 清理字段：去除多余的引号和空格
		for j, field := range fields {
			field = strings.TrimSpace(field)
			// 如果字段被引号包围，去除引号
			if len(field) >= 2 && field[0] == '"' && field[len(field)-1] == '"' {
				field = field[1 : len(field)-1]
				// 处理转义的引号
				field = strings.ReplaceAll(field, `""`, `"`)
			}
			fields[j] = field
		}

		result = append(result, fields)

		// 如果解析失败超过100行，停止解析
		if i > 100 && len(result) == 0 {
			return nil, fmt.Errorf("无法解析CSV文件：格式不正确")
		}
	}

	if len(result) == 0 {
		return nil, fmt.Errorf("CSV文件为空或格式无法识别")
	}

	return result, nil
}

// containsGarbledText 检测是否包含乱码文本
func containsGarbledText(data [][]string) bool {
	// 检查前几行是否包含类似乱码的字符
	checkRows := 3
	if len(data) < checkRows {
		checkRows = len(data)
	}

	for i := 0; i < checkRows; i++ {
		for _, cell := range data[i] {
			// 检查是否包含典型的编码错误字符
			for _, char := range cell {
				// 检查是否为替换字符(U+FFFD)或其他异常字符
				if char == '�' {
					return true
				}
			}
			// 检查特定的GBK乱码模式
			if strings.Contains(cell, "��") || strings.Contains(cell, "Ŀ") {
				return true
			}
		}
	}
	return false
}

// generateCSVFromTasks 从任务生成CSV内容
func generateCSVFromTasks(flow *gaia.BatchWorkflow, tasks []gaia.BatchWorkflowTask) string {
	if len(tasks) == 0 {
		return ""
	}

	// 解析第一个任务的输入参数来获取列名
	var firstTaskInputs map[string]string
	if err := json.Unmarshal([]byte(tasks[0].Inputs), &firstTaskInputs); err != nil {
		return ""
	}

	buf := &bytes.Buffer{}
	w := csv.NewWriter(buf)

	// 标题：输入列 + 处理结果 + 状态
	var nameList []string
	var keyMap map[string]string
	_ = json.Unmarshal([]byte(flow.KeyName), &keyMap)
	headers := make([]string, 0, len(keyMap))
	for key, value := range keyMap {
		headers = append(headers, key)
		nameList = append(nameList, value)
	}
	headers = append(headers, "生成结果")
	_ = w.Write(headers)

	// 行数据
	for _, task := range tasks {
		var inputs map[string]string
		if err := json.Unmarshal([]byte(task.Inputs), &inputs); err != nil {
			continue
		}
		var text string
		row := make([]string, 0, len(headers))
		var result request.WorkflowBatchProcessing
		for _, value := range nameList {
			row = append(row, inputs[value])
		}
		if err := json.Unmarshal([]byte(task.Result), &result); err == nil {
			for key, v := range result.Outputs {
				if key == "task_id" {
					continue
				}
				switch vv := v.(type) {
				case string:
					text += fmt.Sprintf("%s\r", vv)
				case float64:
					text += fmt.Sprintf("%s\r", strconv.FormatFloat(vv, 'f', -1, 64))
				case int64:
					text += fmt.Sprintf("%d\r", vv)
				}

			}
		}
		row = append(row, text)
		_ = w.Write(row)
	}

	w.Flush()
	return buf.String()
}

// GetWorkerPoolStatus 获取工作池状态
// @Tags BatchWorkflow
// @Summary 获取工作池状态
// @Description 获取当前工作池的运行状态和统计信息
// @Accept application/json
// @Produce application/json
// @Success 200 {object} response.Response{data=map[string]interface{}} "成功"
// @Router /gaia/workflow/worker-pool/status [get]
func (api *BatchWorkflowApi) GetWorkerPoolStatus(c *gin.Context) {
	pool := batchWorkflowService.GetWorkerPool()
	if pool == nil {
		response.FailWithMessage("工作池未初始化", c)
		return
	}

	status := pool.GetStatus()
	response.OkWithData(status, c)
}

// RestartWorkerPool 重启工作池
// @Tags BatchWorkflow
// @Summary 重启工作池
// @Description 停止当前工作池并重新启动
// @Accept application/json
// @Produce application/json
// @Param workers query int false "worker数量" default(5)
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/worker-pool/restart [post]
func (api *BatchWorkflowApi) RestartWorkerPool(c *gin.Context) {
	workers := global.GVA_CONFIG.System.WorkFlowNumber
	if workersParam := c.Query("workers"); workersParam != "" {
		if w, err := strconv.Atoi(workersParam); err == nil && w > 0 && w <= 20 {
			workers = w
		}
	}

	// 停止当前工作池
	batchWorkflowService.StopWorkerPool()

	// 启动新的工作池
	batchWorkflowService.InitWorkerPool(workers)

	response.OkWithMessage("工作池重启成功", c)
}

// StopWorkerPool 停止工作池
// @Tags BatchWorkflow
// @Summary 停止工作池
// @Description 停止当前工作池
// @Accept application/json
// @Produce application/json
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/worker-pool/stop [post]
func (api *BatchWorkflowApi) StopWorkerPool(c *gin.Context) {
	batchWorkflowService.StopWorkerPool()
	response.OkWithMessage("工作池已停止", c)
}

// StartWorkerPool 启动工作池
// @Tags BatchWorkflow
// @Summary 启动工作池
// @Description 启动工作池
// @Accept application/json
// @Produce application/json
// @Param workers query int false "worker数量" default(5)
// @Success 200 {object} response.Response "成功"
// @Router /gaia/workflow/worker-pool/start [post]
func (api *BatchWorkflowApi) StartWorkerPool(c *gin.Context) {
	workers := global.GVA_CONFIG.System.WorkFlowNumber
	if workersParam := c.Query("workers"); workersParam != "" {
		if w, err := strconv.Atoi(workersParam); err == nil && w > 0 && w <= 20 {
			workers = w
		}
	}

	batchWorkflowService.InitWorkerPool(workers)
	response.OkWithMessage("工作池启动成功", c)
}

// GetBatchWorkflowList 获取最近30天的批量工作流列表
// @Tags BatchWorkflow
// @Summary 获取最近30天的批量工作流列表
// @Description 获取指定用户最近30天的批量工作流列表，支持分页和按应用过滤
// @Accept application/json
// @Produce application/json
// @Param installed_id query string false "安装的应用ID"
// @Param page query int false "页码" default(1)
// @Param limit query int false "每页数量" default(10)
// @Success 200 {object} response.Response{data=map[string]interface{}} "成功"
// @Router /gaia/workflow/batch/list [get]
func (api *BatchWorkflowApi) GetBatchWorkflowList(c *gin.Context) {
	userID := utils.GetUserID(c)
	installedID := c.Query("installed_id")

	// 解析分页参数
	page := 1
	limit := 10

	if pageParam := c.Query("page"); pageParam != "" {
		if p, err := strconv.Atoi(pageParam); err == nil && p > 0 {
			page = p
		}
	}

	if limitParam := c.Query("limit"); limitParam != "" {
		if l, err := strconv.Atoi(limitParam); err == nil && l > 0 && l <= 100 {
			limit = l
		}
	}

	// 调用服务层方法
	batchWorkflows, total, err := batchWorkflowService.GetBatchWorkflowList(userID, installedID, page, limit)
	if err != nil {
		response.FailWithMessage("获取批量工作流列表失败: "+err.Error(), c)
		return
	}

	// 计算分页信息
	totalPages := (total + int64(limit) - 1) / int64(limit)
	hasMore := int64(page) < totalPages

	response.OkWithData(map[string]interface{}{
		"items":       batchWorkflows,
		"total":       total,
		"page":        page,
		"limit":       limit,
		"total_pages": totalPages,
		"has_more":    hasMore,
	}, c)
}
