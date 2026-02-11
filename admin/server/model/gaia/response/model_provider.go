package response

// ProviderCredentials 提供商凭证（内部/代理用）
type ProviderCredentials struct {
	APIKey   string `json:"api_key"`
	Endpoint string `json:"endpoint,omitempty"`
}

// ModelInfo 模型信息
type ModelInfo struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// ProviderListItem 提供商列表项
type ProviderListItem struct {
	ProviderName    string      `json:"provider_name"`
	Enabled         bool        `json:"enabled"`
	Models          []string    `json:"models"`
	AvailableModels []ModelInfo `json:"available_models"`
}

// OpenAIModelsResponse OpenAI 格式的模型列表响应
type OpenAIModelsResponse struct {
	Data []ModelInfo `json:"data"`
}

// OpenAIModelListItem GET /v1/models 返回的单项
type OpenAIModelListItem struct {
	ID string `json:"id"`
}

// OpenAIModelsListResponse GET /v1/models 接口响应
type OpenAIModelsListResponse struct {
	Data []OpenAIModelListItem `json:"data"`
}

// TongyiModelsListResponse 通义 GET /v1/models 返回的格式：success + output.models
type TongyiModelsListResponse struct {
	Success bool `json:"success"`
	Output  struct {
		Total   int               `json:"total"`
		PageNo  int               `json:"page_no"`
		PageSize int              `json:"page_size"`
		Models  []TongyiModelItem `json:"models"`
	} `json:"output"`
}

// TongyiModelItem 通义模型列表单项，id 为 model 字段
type TongyiModelItem struct {
	Model string `json:"model"`
	Name  string `json:"name"`
}

// GeminiModelsListResponse Google Gemini GET /v1beta/models 返回：models[] + nextPageToken
type GeminiModelsListResponse struct {
	Models         []GeminiModelItem `json:"models"`
	NextPageToken  string            `json:"nextPageToken"`
}

// GeminiModelItem Gemini 模型单项，name 为 "models/gemini-xxx"，baseModelId 用于请求
type GeminiModelItem struct {
	Name        string `json:"name"`
	BaseModelID string `json:"baseModelId"`
	DisplayName string `json:"displayName"`
}
