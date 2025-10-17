package initialize

import (
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/service/gaia"
)

// InitWorkerPool 初始化工作池
func InitWorkerPool() {
	// 从配置中获取worker数量，默认为5
	workerCount := 5
	if global.GVA_CONFIG.System.WorkFlowNumber > 0 {
		workerCount = global.GVA_CONFIG.System.WorkFlowNumber
	}
	global.GVA_LOG.Info(fmt.Sprintf("正在启动批量任务工作池，工作器数量: %d", workerCount))
	gaia.InitWorkerPool(workerCount)
	global.GVA_LOG.Info("批量任务工作池启动完成")
}

// StopWorkerPool 停止工作池（优雅关闭时调用）
func StopWorkerPool() {
	global.GVA_LOG.Info("正在停止批量任务工作池...")
	gaia.StopWorkerPool()
	global.GVA_LOG.Info("批量任务工作池已停止")
}
