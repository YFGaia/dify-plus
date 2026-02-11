package gaia

import (
	"github.com/gin-gonic/gin"
)

type SystemRouter struct{}

// InitSystemRouter 初始化系统路由
func (s *SystemRouter) InitSystemRouter(Router *gin.RouterGroup) {
	systemRouter := Router.Group("gaia/system")
	{
		systemRouter.GET("dingtalk", systemApi.GetDingTalk)          // 获取钉钉系统配置
		systemRouter.POST("dingtalk", systemApi.SetDingTalk)         // 设置钉钉系统配置
		systemRouter.GET("oauth2", systemOAuth2Api.GetOAuth2Config)  // 获取OAuth2配置
		systemRouter.POST("oauth2", systemOAuth2Api.SetOAuth2Config) // 设置OAuth2配置
	}
}

// InitModelProviderRouter 初始化模型提供商路由
func (s *SystemRouter) InitModelProviderRouter(Router *gin.RouterGroup) {
	// 管理端API（需要JWT认证）
	modelProviderRouter := Router.Group("gaia/model-provider")
	{
		modelProviderRouter.GET("list", modelProviderApi.GetProviderList)                     // 获取提供商配置列表
		modelProviderRouter.POST("update", modelProviderApi.UpdateProviderConfig)             // 更新提供商配置
		modelProviderRouter.GET("available-models", modelProviderApi.GetAvailableModels)      // 获取可用模型
		modelProviderRouter.GET("test-credentials", modelProviderApi.TestProviderCredentials) // 测试凭证
		modelProviderRouter.GET("logs", modelProviderApi.GetProxyLogs)                        // 获取代理日志
	}

	// 第三方API（需要JWT认证）
	gaiaRouter := Router.Group("gaia")
	{
		gaiaRouter.GET("models", modelProviderApi.GetModels)  // 获取开启的模型列表（OpenAI格式）
		gaiaRouter.Any("proxy/*path", modelProviderApi.Proxy) // 通用中转API：按路径转发（v1/chat/completions、v1/messages、v1/images/generations、v1/embeddings 等）
	}
}
