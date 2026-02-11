package gaia

// 模型提供商逻辑名称（列表展示与内部 key）
const (
	ProviderOpenai   = "openai"
	ProviderTongyi   = "tongyi"
	ProviderGoogle   = "google"
	ProviderAnthropic = "anthropic"
)

// DifyProviderTypeCustom Dify providers 表 provider_type 枚举
const DifyProviderTypeCustom = "custom"

// 凭证配置中的 key 名
const (
	ConfigKeyOpenaiAPIKey    = "openai_api_key"
	ConfigKeyOpenaiAPIBase   = "openai_api_base"
	ConfigKeyDashScopeAPIKey = "dashscope_api_key"
	ConfigKeyAPIKey          = "api_key"
)

// SupportedProviders 列表展示的提供商顺序
var SupportedProviders = []string{ProviderOpenai, ProviderTongyi, ProviderGoogle, ProviderAnthropic}

// DefaultChatCompletionsEndpoints 各提供商聊天接口默认完整 URL（兼容旧 ProxyChat）
var DefaultChatCompletionsEndpoints = map[string]string{
	ProviderOpenai: "https://api.openai.com/v1/chat/completions",
	ProviderTongyi: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
	ProviderGoogle: "https://generativelanguage.googleapis.com/v1beta/chat/completions",
}

// DefaultAPIBase 各提供商 API 根地址（无路径，用于通用代理；当 provider_credentials.encrypted_config 无 openai_api_base 时使用）
var DefaultAPIBase = map[string]string{
	ProviderOpenai:    "https://api.openai.com",
	ProviderTongyi:    "https://dashscope.aliyuncs.com/compatible-mode",
	ProviderGoogle:    "https://generativelanguage.googleapis.com",
	ProviderAnthropic: "https://api.anthropic.com",
}

// CredentialKeyFallback 未知提供商时依次尝试的配置 key
var CredentialKeyFallback = []string{ConfigKeyOpenaiAPIKey, ConfigKeyAPIKey, ConfigKeyDashScopeAPIKey}
