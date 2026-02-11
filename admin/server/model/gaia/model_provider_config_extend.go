package gaia

import "time"

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
