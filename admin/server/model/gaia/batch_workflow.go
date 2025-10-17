package gaia

import "time"

// 批量工作流状态常量
const (
	BatchWorkflowStatusPending    = "pending"    // 待处理
	BatchWorkflowStatusProcessing = "processing" // 处理中
	BatchWorkflowStatusCompleted  = "completed"  // 已完成
	BatchWorkflowStatusFailed     = "failed"     // 失败
	BatchWorkflowStatusStopped    = "stopped"    // 已停止
)

// 批量工作流任务状态常量
const (
	BatchTaskStatusPending    = "pending"    // 待处理
	BatchTaskStatusQueued     = "queued"     // 队列中
	BatchTaskStatusRunning    = "running"    // 运行中
	BatchTaskStatusCompleted  = "completed"  // 已完成
	BatchTaskStatusFailed     = "failed"     // 失败
	BatchTaskStatusCancelled  = "cancelled"  // 已取消
)

// 批量工作流错误消息常量
const (
	ErrorInsufficientBalance = "余额不足，调用失败！"
	ErrorMaxRetryExceeded    = "重试超过3次"
	ErrorWorkflowFailed      = "工作流执行失败"
	ErrorCallAPIFailed       = "调用Dify API失败"
	ErrorParseResultFailed   = "解析API返回结果失败"
)

// 批量工作流配置常量
const (
	MaxTaskRetryCount      = 3  // 最大任务重试次数
	ErrorPenaltyThreshold  = 50 // 错误惩罚阈值（每50个错误减少1个并发位）
)

// BatchWorkflow 批量工作流处理
type BatchWorkflow struct {
	ID            string    `json:"id" gorm:"primaryKey;comment:批量处理ID"`
	UserID        uint      `json:"user_id" gorm:"index;comment:用户id"`
	InstalledID   string    `json:"installed_id" gorm:"not null;comment:安装的应用ID"`
	FileName      string    `json:"file_name" gorm:"not null;comment:上传的文件名"`
	TotalRows     int       `json:"total_rows" gorm:"not null;default:0;comment:总行数"`
	ProcessedRows int       `json:"processed_rows" gorm:"not null;default:0;comment:已处理行数"`
	Status        string    `json:"status" gorm:"not null;default:'pending';comment:状态: pending, processing, completed, failed, stopped"`
	Results       string    `json:"results" gorm:"type:text;comment:处理结果"`
	KeyName       string    `json:"key_name" gorm:"type:text;comment:键名"`
	Error         string    `json:"error" gorm:"comment:错误信息"`
	ErrorCount    int       `json:"error_count" gorm:"not null;default:0;comment:累计错误次数"`
	CreatedAt     time.Time `json:"created_at" gorm:"not null;default:CURRENT_TIMESTAMP(0);comment:创建时间"`
	UpdatedAt     time.Time `json:"updated_at" gorm:"not null;default:CURRENT_TIMESTAMP(0);comment:更新时间"`
}

// BatchWorkflowTask 批量工作流任务
type BatchWorkflowTask struct {
	ID              string    `json:"id" gorm:"primaryKey;comment:任务ID"`
	BatchWorkflowID string    `json:"batch_workflow_id" gorm:"not null;comment:批量处理ID"`
	RowIndex        int       `json:"row_index" gorm:"not null;comment:行索引"`
	Inputs          string    `json:"inputs" gorm:"type:text;comment:输入参数"`
	Status          string    `json:"status" gorm:"not null;default:'pending';comment:状态: pending, running, completed, failed, cancelled"`
	Result          string    `json:"result" gorm:"type:text;comment:处理结果"`
	Error           string    `json:"error" gorm:"comment:错误信息"`
	ErrorCount      int       `json:"error_count" gorm:"not null;default:0;comment:错误次数"`
	CreatedAt       time.Time `json:"created_at" gorm:"not null;default:CURRENT_TIMESTAMP(0);comment:创建时间"`
	UpdatedAt       time.Time `json:"updated_at" gorm:"not null;default:CURRENT_TIMESTAMP(0);comment:更新时间"`
}

func (BatchWorkflow) TableName() string     { return "batch_workflows_extend" }
func (BatchWorkflowTask) TableName() string { return "batch_workflow_tasks_extend" }
