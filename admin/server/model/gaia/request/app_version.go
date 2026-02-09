package request

// AppVersionTokenConfig 仅设置链接 Token（全局）
type AppVersionTokenConfig struct {
	LinkToken *string `json:"link_token,omitempty"` // 空或 "********" 不更新，"" 清除
}

// AppVersionTokenReveal 输入登录密码以查看明文 Token
type AppVersionTokenReveal struct {
	Password string `json:"password" binding:"required"`
}

// AppVersionReleaseCreate 新增版本
type AppVersionReleaseCreate struct {
	Version      string `json:"version" binding:"required"` // 版本号
	ReleaseNotes string `json:"release_notes"`              // 更新说明
}

// AppVersionReleaseUpdate 更新版本信息
type AppVersionReleaseUpdate struct {
	Version      string `json:"version" binding:"required"`
	ReleaseNotes string `json:"release_notes"`
}

// AppVersionDeleteQuery 删除某平台/架构包
type AppVersionDeleteQuery struct {
	Platform string `form:"platform" binding:"required,oneof=darwin win32 linux"`
	Arch     string `form:"arch" binding:"required,oneof=x64 arm64"`
}
