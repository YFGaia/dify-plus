package response

import (
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
)

// GaiaLoginResult 登录结果（含 JWT 与第三方回调参数）
type GaiaLoginResult struct {
	User        system.SysUser `json:"user"`
	Token       string         `json:"token"`
	RedirectURI string         `json:"redirect_uri,omitempty"`
	State       string         `json:"state,omitempty"`
}

// LoginOptionsResponse 登录方式选项（公开，不包含密钥）
type LoginOptionsResponse struct {
	DingTalk struct {
		Enabled bool   `json:"enabled"`
		AuthURL string `json:"auth_url,omitempty"`
	} `json:"dingtalk"`
	OAuth2 struct {
		Enabled     bool   `json:"enabled"`
		AuthURL     string `json:"auth_url,omitempty"`
		RedirectURI string `json:"redirect_uri,omitempty"`
	} `json:"oauth2"`
}
