package gaia

import (
	"github.com/gin-gonic/gin"
)

type AppVersionRouter struct{}

// InitAppVersionRouter 公开：GET /latest、GET /releases（版本列表）；鉴权：/gaia/app-version/*
func (r *AppVersionRouter) InitAppVersionRouter(publicGroup, privateGroup *gin.RouterGroup) {
	publicGroup.GET("/latest", appVersionApi.GetLatest)
	publicGroup.GET("/releases", appVersionApi.ListReleases) // 公开：版本列表，无需登录

	appVersion := privateGroup.Group("gaia/app-version")
	{
		appVersion.GET("token", appVersionApi.GetTokenConfig) // 全局 Token 配置（脱敏）
		appVersion.PUT("token", appVersionApi.SetTokenConfig)
		appVersion.POST("token/reveal", appVersionApi.RevealToken)               // 密码验证后查看明文 Token
		appVersion.GET("releases", appVersionApi.ListReleases)                   // 版本列表
		appVersion.POST("releases", appVersionApi.CreateRelease)                 // 新增版本
		appVersion.GET("releases/:id", appVersionApi.GetRelease)                 // 版本详情
		appVersion.PUT("releases/:id", appVersionApi.UpdateRelease)              // 更新版本信息
		appVersion.POST("releases/:id/upload", appVersionApi.UploadToRelease)    // 上传安装包（自动识别平台/架构）
		appVersion.DELETE("releases/:id/download", appVersionApi.DeleteDownload) // 删除某包
	}
}
