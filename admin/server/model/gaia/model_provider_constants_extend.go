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
	ProviderAWS       = "aws" // AWS Bedrock 渠道（用于转发 Claude 等 Anthropic 模型）
)

// DifyProviderTypeCustom Dify providers 表 provider_type 枚举
const DifyProviderTypeCustom = "custom"

// 凭证配置中的 key 名
const (
	ConfigKeyOpenaiAPIKey     = "openai_api_key"
	ConfigKeyOpenaiAPIBase    = "openai_api_base"
	ConfigKeyOpenaiAPIVersion = "openai_api_version"
	ConfigKeyDashScopeAPIKey  = "dashscope_api_key"
	ConfigKeyAPIKey           = "api_key"

	// AWS Bedrock 凭证字段（Dify bedrock provider 的 encrypted_config 中使用）
	ConfigKeyAWSAccessKeyID     = "aws_access_key_id"
	ConfigKeyAWSSecretAccessKey = "aws_secret_access_key"
	ConfigKeyAWSSessionToken    = "aws_session_token"
	ConfigKeyAWSRegion          = "aws_region"
)

// SupportedProviders 列表展示的提供商顺序
var SupportedProviders = []string{ProviderOpenai, ProviderTongyi, ProviderGoogle, ProviderAnthropic, ProviderAWS, ProviderAzure, ProviderZhipuai, ProviderMinimax}

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
	ProviderAWS:       "https://bedrock-runtime.us-east-1.amazonaws.com",
	ProviderZhipuai:   "https://open.bigmodel.cn",
	ProviderMinimax:   "https://api.minimax.chat",
	// Azure 的 base URL 来自 openai_api_base 配置，不设置默认值
}

// CredentialKeyFallback 未知提供商时依次尝试的配置 key
var CredentialKeyFallback = []string{ConfigKeyOpenaiAPIKey, ConfigKeyAPIKey, ConfigKeyDashScopeAPIKey}

// RmbToUSDRate 人民币兑美元汇率
const RmbToUSDRate = 7.26

// DefaultImageGenerationPriceUSD 图片生成等按次计费接口的默认单价（USD），无 usage 时使用
const DefaultImageGenerationPriceUSD = 0.04

// DefaultQuotaFallbackUSDPerToken 未命中定价时的兜底单价：每 token 的 USD 金额（仅做记账占位，约 $0.001/千 token）
const DefaultQuotaFallbackUSDPerToken = 0.000001

// Gaia 相关 Redis Key（GVA_REDIS / GVA_Dify_REDIS）
const (
	RedisKeyGaiaAdminConsoleToken          = "gaia:admin_console_token"
	RedisKeyGaiaModelPricingPrefix         = "gaia:model_pricing:"
	RedisKeyGaiaForwardDingPrefix          = "gaia:forward:ding:"
	RedisKeyModelProviderCredentialsPrefix = "model_provider_credentials:"
)

// BuiltinModelPricing 内置兜底定价表（当 Dify Console API 未返回该模型定价时使用）。
// 价格单位：每千 token（与 ModelPricing.Unit=0.001 对应），货币为各模型实际结算货币。
// 通义/百炼模型官方定价（人民币，参考 https://help.aliyun.com/document_detail/2586379.html）：
//   - 输入/输出价格均为「每百万 token」，换算为每千 token 时除以 1000。
var BuiltinModelPricing = map[string]ModelPricing{
	// ──── 通义千问 Qwen3 系列（RMB / 百万 token，128K 档） ────
	"qwen3-235b-a22b": {Input: 0.4 / 1000, Output: 1.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-30b-a3b":   {Input: 0.11 / 1000, Output: 0.44 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-32b":       {Input: 0.8 / 1000, Output: 3.2 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-14b":       {Input: 0.3 / 1000, Output: 1.2 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-8b":        {Input: 0.1 / 1000, Output: 0.4 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-4b":        {Input: 0.04 / 1000, Output: 0.16 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-1.7b":      {Input: 0.02 / 1000, Output: 0.08 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3-0.6b":      {Input: 0.01 / 1000, Output: 0.04 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── 通义千问 Qwen3.5 系列（RMB / 百万 token，128K 档） ────
	"qwen3.5-plus":  {Input: 0.8 / 1000, Output: 4.8 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen3.5-turbo": {Input: 0.3 / 1000, Output: 1.2 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── 通义千问 Qwen2.5 系列（RMB / 百万 token） ────
	"qwen2.5-72b-instruct": {Input: 4.0 / 1000, Output: 12.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-32b-instruct": {Input: 3.5 / 1000, Output: 7.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-14b-instruct": {Input: 2.0 / 1000, Output: 6.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-7b-instruct":  {Input: 1.0 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen2.5-3b-instruct":  {Input: 0.3 / 1000, Output: 0.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-plus":            {Input: 0.8 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-turbo":           {Input: 0.3 / 1000, Output: 0.6 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-max":             {Input: 40.0 / 1000, Output: 120.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"qwen-long":            {Input: 0.5 / 1000, Output: 2.0 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── 月之暗面 Kimi 系列（RMB / 百万 token） ────
	// Kimi 走 tongyi（百炼）渠道转发，命名沿用 kimi 前缀；前缀匹配会让 kimi2-k2.6-xxx 也命中 kimi2-k2.6
	"kimi2-k2.6":       {Input: 4.0 / 1000, Output: 16.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"moonshot-v1-8k":   {Input: 12.0 / 1000, Output: 12.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"moonshot-v1-32k":  {Input: 24.0 / 1000, Output: 24.0 / 1000, Unit: 0.001, Currency: "RMB"},
	"moonshot-v1-128k": {Input: 60.0 / 1000, Output: 60.0 / 1000, Unit: 0.001, Currency: "RMB"},

	// ──── Anthropic Claude 系列（USD / 百万 token） ────
	// Claude 4.6 / 4.7 系列（Sonnet 与 Opus）；anthropic 直连与 AWS Bedrock 走同一份定价
	"claude-sonnet-4-6": {Input: 3.0 / 1000, Output: 15.0 / 1000, Unit: 0.001, Currency: "USD"},
	"claude-sonnet-4-7": {Input: 3.0 / 1000, Output: 15.0 / 1000, Unit: 0.001, Currency: "USD"},
	"claude-opus-4-6":   {Input: 15.0 / 1000, Output: 75.0 / 1000, Unit: 0.001, Currency: "USD"},
	"claude-opus-4-7":   {Input: 15.0 / 1000, Output: 75.0 / 1000, Unit: 0.001, Currency: "USD"},
	// AWS Bedrock 上常用的模型 ID 形式（带 anthropic. 前缀与 -v1:0 后缀），单独列出避免前缀匹配漂移
	"anthropic.claude-sonnet-4-6-v1:0": {Input: 3.0 / 1000, Output: 15.0 / 1000, Unit: 0.001, Currency: "USD"},
	"anthropic.claude-sonnet-4-7-v1:0": {Input: 3.0 / 1000, Output: 15.0 / 1000, Unit: 0.001, Currency: "USD"},
	"anthropic.claude-opus-4-6-v1:0":   {Input: 15.0 / 1000, Output: 75.0 / 1000, Unit: 0.001, Currency: "USD"},
	"anthropic.claude-opus-4-7-v1:0":   {Input: 15.0 / 1000, Output: 75.0 / 1000, Unit: 0.001, Currency: "USD"},

	// ──── OpenAI 图片生成（按次计费，Input 字段表示「每次请求的 USD 单价」） ────
	// 命中分支见 service/gaia/model_provider.go 中的 isImageOrPerRequestPath 与 ProxyRequest 计费逻辑
	"gpt-image-1": {Input: 0.04, Currency: "USD"},
	"gpt-image-2": {Input: 0.05, Currency: "USD"},
}
