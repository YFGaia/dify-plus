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

// BuiltinModelPricing 内置兜底定价表（当 Dify Console API 未返回该模型定价时使用）。
// 价格单位：每千 token（与 ModelPricing.Unit=0.001 对应），货币为各模型实际结算货币。
// 通义/百炼模型官方定价（人民币，参考 https://help.aliyun.com/document_detail/2586379.html）：
//   - 输入/输出价格均为「每百万 token」，换算为每千 token 时除以 1000。
var BuiltinModelPricing = map[string]ModelPricing{
	// ──── 通义千问 Qwen3 系列（RMB / 百万 token，128K 档） ────
	"qwen3-235b-a22b":  {Input: 0.4 / 1000, Output: 1.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-30b-a3b":    {Input: 0.11 / 1000, Output: 0.44 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-32b":        {Input: 0.8 / 1000, Output: 3.2 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-14b":        {Input: 0.3 / 1000, Output: 1.2 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-8b":         {Input: 0.1 / 1000, Output: 0.4 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-4b":         {Input: 0.04 / 1000, Output: 0.16 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-1.7b":       {Input: 0.02 / 1000, Output: 0.08 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-0.6b":       {Input: 0.01 / 1000, Output: 0.04 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── 通义千问 Qwen3.5 系列（RMB / 百万 token，128K 档） ────
	"qwen3.5-plus":     {Input: 0.8 / 1000, Output: 4.8 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3.5-turbo":    {Input: 0.3 / 1000, Output: 1.2 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── 通义千问 Qwen2.5 系列（RMB / 百万 token） ────
	"qwen2.5-72b-instruct":   {Input: 4.0 / 1000, Output: 12.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-32b-instruct":   {Input: 3.5 / 1000, Output: 7.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-14b-instruct":   {Input: 2.0 / 1000, Output: 6.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-7b-instruct":    {Input: 1.0 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-3b-instruct":    {Input: 0.3 / 1000, Output: 0.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-plus":              {Input: 0.8 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-turbo":             {Input: 0.3 / 1000, Output: 0.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-max":               {Input: 40.0 / 1000, Output: 120.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-long":              {Input: 0.5 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},
}
