package gaia

import (
	"github.com/gin-gonic/gin"
)

type WorkflowRouter struct{}

// InitWorkflowRouter 初始化批量处理工作流路由
func (w *WorkflowRouter) InitWorkflowRouter(Router *gin.RouterGroup) {
	workflowRouter := Router.Group("gaia/workflow")
	{
		// 批量处理工作流相关路由
		workflowRouter.POST("batch/processing", batchWorkflowApi.CreateBatchWorkflow)           // 创建批量处理
		workflowRouter.GET("batch/list", batchWorkflowApi.GetBatchWorkflowList)                 // 获取最近30天的批量工作流列表
		workflowRouter.GET("batch/:id", batchWorkflowApi.GetBatchWorkflow)                      // 获取批量处理信息
		workflowRouter.GET("batch/:id/tasks", batchWorkflowApi.GetBatchWorkflowTasks)           // 获取任务列表
		workflowRouter.GET("batch/:id/progress", batchWorkflowApi.GetBatchWorkflowProgress)     // 获取进度信息
		workflowRouter.POST("batch/:id/stop", batchWorkflowApi.StopBatchWorkflow)               // 停止批量处理
		workflowRouter.POST("batch/:id/retry", batchWorkflowApi.RetryBatchWorkflow)             // 重试批量处理（重新开始所有任务）
		workflowRouter.POST("batch/:id/retry-failed", batchWorkflowApi.RetryFailedTasks)        // 仅重试失败的任务
		workflowRouter.POST("batch/:id/resume", batchWorkflowApi.ResumeBatchWorkflow)           // 恢复批量处理
		workflowRouter.GET("batch/:id/download", batchWorkflowApi.DownloadBatchWorkflowResults) // 下载结果

		// 工作池管理相关路由
		workflowRouter.GET("worker-pool/status", batchWorkflowApi.GetWorkerPoolStatus) // 获取工作池状态
		workflowRouter.POST("worker-pool/restart", batchWorkflowApi.RestartWorkerPool) // 重启工作池
		workflowRouter.POST("worker-pool/stop", batchWorkflowApi.StopWorkerPool)       // 停止工作池
		workflowRouter.POST("worker-pool/start", batchWorkflowApi.StartWorkerPool)     // 启动工作池

		// 错误计数重置相关路由
		workflowRouter.POST("batch/:id/reset-error-count", batchWorkflowApi.ResetBatchWorkflowErrorCount) // 重置批量工作流错误计数
		workflowRouter.POST("batch/reset-user-error-count", batchWorkflowApi.ResetUserErrorCount)         // 重置用户所有批量工作流错误计数
	}
}
