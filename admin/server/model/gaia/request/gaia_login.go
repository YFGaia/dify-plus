package request

// GaiaOAuth2LoginReq OAuth2 登录请求（code 与 access_token 二选一；Extend: access_token 兼容 casdoor implicit/hybrid）
type GaiaOAuth2LoginReq struct {
	Code        string `json:"code"`
	AccessToken string `json:"access_token"` // Extend: 兼容 casdoor，无 code 时直接使用回调中的 access_token
	RedirectURI string `json:"redirect_uri"`
	State       string `json:"state"`
}

// GaiaDingTalkLoginReq 钉钉登录请求
type GaiaDingTalkLoginReq struct {
	AuthCode    string `json:"auth_code" binding:"required"`
	RedirectURI string `json:"redirect_uri"`
	State       string `json:"state"`
}
