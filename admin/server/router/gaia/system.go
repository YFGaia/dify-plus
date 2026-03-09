package gaia

import (
	"github.com/gin-gonic/gin"
)

type SystemRouter struct{}

// InitSystemRouter 初始化系统路由
func (s *SystemRouter) InitSystemRouter(Router *gin.RouterGroup) {
	systemRouter := Router.Group("gaia/system")
	{
		systemRouter.GET("dingtalk", systemApi.GetDingTalk)             // 获取钉钉系统配置
		systemRouter.POST("dingtalk", systemApi.SetDingTalk)            // 设置钉钉系统配置
		systemRouter.GET("oauth2", systemOAuth2Api.GetOAuth2Config)     // 获取 OAuth2 配置
		systemRouter.POST("oauth2", systemOAuth2Api.SetOAuth2Config)    // 设置 OAuth2 配置
		// 转发 Token 管理
		systemRouter.GET("forward-tokens", systemApi.GetForwardTokens)          // 获取转发 Token 列表
		systemRouter.POST("forward-tokens", systemApi.CreateForwardToken)       // 新增转发 Token
		systemRouter.DELETE("forward-tokens/:id", systemApi.DeleteForwardToken) // 删除转发 Token
	}
}

// InitForwardProxyRouter 初始化 GPT 转发代理路由（免 JWT，挂在 PublicGroup）
func (s *SystemRouter) InitForwardProxyRouter(PublicRouter *gin.RouterGroup) {
	// 免 JWT 转发入口，通过 forwarding token + ding_id 鉴权
	PublicRouter.Any("gaia/forward/proxy/*path", forwardProxyApi.ForwardProxy)
}

// InitModelProviderRouter 初始化模型提供商路由
func (s *SystemRouter) InitModelProviderRouter(Router *gin.RouterGroup) {
	// 管理端 API（需要 JWT 认证）
	modelProviderRouter := Router.Group("gaia/model-provider")
	{
		modelProviderRouter.GET("list", modelProviderApi.GetProviderList)                     // 获取提供商配置列表
		modelProviderRouter.POST("update", modelProviderApi.UpdateProviderConfig)             // 更新提供商配置
		modelProviderRouter.GET("available-models", modelProviderApi.GetAvailableModels)      // 获取可用模型
		modelProviderRouter.GET("test-credentials", modelProviderApi.TestProviderCredentials) // 测试凭证
		modelProviderRouter.GET("logs", modelProviderApi.GetProxyLogs)                        // 获取代理日志
	}

	// 第三方 API（需要 JWT 认证）
	gaiaRouter := Router.Group("gaia")
	{
		gaiaRouter.GET("models", modelProviderApi.GetModels)  // 获取开启的模型列表（OpenAI 格式）
		gaiaRouter.Any("proxy/*path", modelProviderApi.Proxy) // 通用中转 API：按路径转发（v1/chat/completions、v1/messages、v1/images/generations、v1/embeddings 等）
	}
}
