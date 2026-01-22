package request

type WorkflowBatchProcessing struct {
	Outputs map[string]interface{} `json:"outputs" gorm:"comment:从任务生成CSV内容"` // 从任务生成CSV内容
}

// SSEEvent 表示一个SSE事件
type SSEEvent struct {
	Event string                 `json:"event"`
	Data  map[string]interface{} `json:"data"`
}

// NodeExecution 表示节点执行信息
type NodeExecution struct {
	ID          string                 `json:"id"`
	NodeID      string                 `json:"node_id"`
	NodeType    string                 `json:"node_type"`
	Title       string                 `json:"title"`
	Index       int                    `json:"index"`
	Status      string                 `json:"status"`
	Error       string                 `json:"error,omitempty"`
	ElapsedTime float64                `json:"elapsed_time"`
	Inputs      map[string]interface{} `json:"inputs,omitempty"`
	Outputs     map[string]interface{} `json:"outputs,omitempty"`
	CreatedAt   int64                  `json:"created_at"`
	FinishedAt  int64                  `json:"finished_at,omitempty"`
}

// WorkflowResult 表示工作流执行结果
type WorkflowResult struct {
	WorkflowRunID   string                 `json:"workflow_run_id"`
	WorkflowID      string                 `json:"workflow_id"`
	SequenceNumber  int                    `json:"sequence_number"`
	Status          string                 `json:"status"`
	Outputs         map[string]interface{} `json:"outputs"`
	Error           string                 `json:"error,omitempty"`
	ElapsedTime     float64                `json:"elapsed_time"`
	TotalTokens     int                    `json:"total_tokens"`
	TotalSteps      int                    `json:"total_steps"`
	ExceptionsCount int                    `json:"exceptions_count"`
	CreatedAt       int64                  `json:"created_at"`
	FinishedAt      int64                  `json:"finished_at,omitempty"`
	Nodes           []NodeExecution        `json:"nodes"`
}
