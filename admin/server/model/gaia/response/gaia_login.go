package response

import (
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
)

// GaiaLoginResult 登录结果（含 JWT 与第三方回调参数）
type GaiaLoginResult struct {
	User        system.SysUser `json:"user"`
	Token       string        `json:"token"`
	RedirectURI string        `json:"redirect_uri,omitempty"`
	State       string        `json:"state,omitempty"`
}
