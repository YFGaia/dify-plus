package response

import "time"

// ForwardTokenInfo 转发 Token 列表项（不暴露内部 ID）
type ForwardTokenInfo struct {
	ID        string    `json:"id"` // token
	Seq       int       `json:"seq"` // 1..N 序列号（用于删除）
	CreatedAt time.Time `json:"created_at"`
}

// ForwardTokensResponse 获取转发 Token 列表响应
type ForwardTokensResponse struct {
	Tokens []ForwardTokenInfo `json:"tokens"`
	Count  int                `json:"count"`
	Max    int                `json:"max"`
}

// TestEmailApiConfigResponse 测试邮箱 API 配置响应
type TestEmailApiConfigResponse struct {
	StatusCode         int         `json:"status_code"`          // HTTP 状态码
	Body               interface{} `json:"body"`                 // 响应 Body（JSON 时为对象，否则为字符串）
	EmailFieldPreview  string      `json:"email_field_preview"`  // 邮箱字段解析预览（如 data[0].userName = test@example.com）
	IsValid            bool        `json:"is_valid"`             // 配置是否有效（能正确提取邮箱）
	ErrorMessage       string      `json:"error_message,omitempty"` // 错误信息（可选）
}

