package gaia

// 模型提供商逻辑名称（列表展示与内部 key）
const (
	ProviderOpenai    = "openai"
	ProviderTongyi    = "tongyi"
	ProviderGoogle    = "google"
	ProviderAnthropic = "anthropic"
	ProviderAzure     = "azure"
	ProviderZhipuai   = "zhipuai"
	ProviderMinimax   = "minimax"
)

// DifyProviderTypeCustom Dify providers 表 provider_type 枚举
const DifyProviderTypeCustom = "custom"

// 凭证配置中的 key 名
const (
	ConfigKeyOpenaiAPIKey      = "openai_api_key"
	ConfigKeyOpenaiAPIBase     = "openai_api_base"
	ConfigKeyOpenaiAPIVersion  = "openai_api_version"
	ConfigKeyDashScopeAPIKey   = "dashscope_api_key"
	ConfigKeyAPIKey            = "api_key"
)

// SupportedProviders 列表展示的提供商顺序
var SupportedProviders = []string{ProviderOpenai, ProviderTongyi, ProviderGoogle, ProviderAnthropic, ProviderAzure, ProviderZhipuai, ProviderMinimax}

// DefaultChatCompletionsEndpoints 各提供商聊天接口默认完整 URL（兼容旧 ProxyChat）
var DefaultChatCompletionsEndpoints = map[string]string{
	ProviderOpenai:  "https://api.openai.com/v1/chat/completions",
	ProviderTongyi:  "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
	ProviderGoogle:  "https://generativelanguage.googleapis.com/v1beta/chat/completions",
	ProviderZhipuai: "https://open.bigmodel.cn/api/paas/v4/chat/completions",
	ProviderMinimax: "https://api.minimax.chat/v1/text/chatcompletion_v2",
	// Azure 需要动态构建 URL，不使用默认值
}

// DefaultAPIBase 各提供商 API 根地址（无路径，用于通用代理；当 provider_credentials.encrypted_config 无 openai_api_base 时使用）
var DefaultAPIBase = map[string]string{
	ProviderOpenai:    "https://api.openai.com",
	ProviderTongyi:    "https://dashscope.aliyuncs.com/compatible-mode",
	ProviderGoogle:    "https://generativelanguage.googleapis.com",
	ProviderAnthropic: "https://api.anthropic.com",
	ProviderZhipuai:   "https://open.bigmodel.cn",
	ProviderMinimax:   "https://api.minimax.chat",
	// Azure 的 base URL 来自 openai_api_base 配置，不设置默认值
}

// CredentialKeyFallback 未知提供商时依次尝试的配置 key
var CredentialKeyFallback = []string{ConfigKeyOpenaiAPIKey, ConfigKeyAPIKey, ConfigKeyDashScopeAPIKey}
