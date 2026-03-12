package request

// GetProxyLogsReq 代理日志分页请求
type GetProxyLogsReq struct {
	Page     int `form:"page"`      // 页码，从 1 开始
	PageSize int `form:"page_size"` // 每页条数，最大 100
}

// ChatRequest 聊天请求（OpenAI 兼容）
type ChatRequest struct {
	Model       string                   `json:"model"`
	Messages    []map[string]interface{} `json:"messages"`
	Stream      bool                     `json:"stream"`
	Temperature float64                  `json:"temperature,omitempty"`
	MaxTokens   int                      `json:"max_tokens,omitempty"`
	Tools       []map[string]interface{} `json:"tools,omitempty"`
	ToolChoice  interface{}              `json:"tool_choice,omitempty"`
}
