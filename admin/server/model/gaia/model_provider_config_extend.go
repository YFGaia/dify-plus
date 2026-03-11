package gaia

import "time"

// ModelPricing 从 Dify Console API 拉取的模型定价信息（对应 pricing 字段）
type ModelPricing struct {
	Input    float64 `json:"input"`    // 每 unit 的输入单价
	Output   float64 `json:"output"`   // 每 unit 的输出单价（0 表示与 Input 相同或不区分）
	Unit     float64 `json:"unit"`     // 计费单位（通常 0.001，即每千 token）
	Currency string  `json:"currency"` // 货币（USD / RMB）
}

// ModelUsage OpenAI 格式响应中的 usage 字段（非流式及流式末尾行）
type ModelUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
}

// ModelUsageResponse OpenAI 格式响应体（仅用于提取 usage 字段）
type ModelUsageResponse struct {
	Usage *ModelUsage `json:"usage"`
}

// DifyModelPricingRaw Dify Console API 返回的原始定价字段（值为字符串形式的数字）
type DifyModelPricingRaw struct {
	Input    string `json:"input"`
	Output   string `json:"output"`
	Unit     string `json:"unit"`
	Currency string `json:"currency"`
}

// DifyModelItem Dify Console API 返回的单个模型信息
type DifyModelItem struct {
	Model   string               `json:"model"`
	Pricing *DifyModelPricingRaw `json:"pricing"`
}

// DifyProviderModels Dify Console API 返回的单个 provider 下的模型列表
type DifyProviderModels struct {
	Models []DifyModelItem `json:"models"`
}

// DifyModelsResponse Dify Console API GET /models/model-types/llm 的响应结构
type DifyModelsResponse struct {
	Data []DifyProviderModels `json:"data"`
}

// ModelProviderConfig 模型提供商配置表
type ModelProviderConfig struct {
	Id           uint      `json:"id" form:"id" gorm:"primarykey;column:id;comment:id;"`
	ProviderName string    `json:"provider_name" gorm:"unique;not null;column:provider_name;comment:提供商名称"`
	Enabled      bool      `json:"enabled" gorm:"default:false;column:enabled;comment:是否开启"`
	Models       string    `json:"models" gorm:"type:text;column:models;comment:开启的模型列表(JSON数组)"`
	Config       string    `json:"config" gorm:"type:text;column:config;comment:额外配置(JSON)"`
	CreatedAt    time.Time `json:"created_at" gorm:"column:created_at;comment:创建时间"`
	UpdatedAt    time.Time `json:"updated_at" gorm:"column:updated_at;comment:更新时间"`
}

// TableName ModelProviderConfig自定义表名 model_provider_config
func (ModelProviderConfig) TableName() string {
	return "model_provider_config_extend"
}

// ModelProxyLog 模型中转请求日志表
type ModelProxyLog struct {
	Id             uint      `json:"id" form:"id" gorm:"primarykey;column:id;comment:id;"`
	UserId         string    `json:"user_id" gorm:"type:uuid;not null;column:user_id;comment:用户ID"`
	ProviderName   string    `json:"provider_name" gorm:"column:provider_name;comment:提供商"`
	ModelName      string    `json:"model_name" gorm:"column:model_name;comment:模型名"`
	RequestTokens  int       `json:"request_tokens" gorm:"column:request_tokens;comment:请求token数"`
	ResponseTokens int       `json:"response_tokens" gorm:"column:response_tokens;comment:响应token数"`
	Status         string    `json:"status" gorm:"column:status;comment:状态"`
	ErrorMessage   string    `json:"error_message" gorm:"type:text;column:error_message;comment:错误信息"`
	CreatedAt      time.Time `json:"created_at" gorm:"column:created_at;comment:创建时间"`
}

// TableName ModelProxyLog自定义表名 model_proxy_log
func (ModelProxyLog) TableName() string {
	return "model_proxy_log_extend"
}
