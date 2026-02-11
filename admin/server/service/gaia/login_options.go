package gaia

import (
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"net/url"
	"strings"
)

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

// GetLoginOptions 获取登录方式选项（供登录页展示钉钉/OAuth2 按钮，不暴露密钥）
func (e *SystemIntegratedService) GetLoginOptions(frontendOrigin string) (res LoginOptionsResponse) {
	// 钉钉
	integrateDing := e.getIntegratedConfigRaw(gaia.SystemIntegrationDingTalk)
	if integrateDing.Status && integrateDing.AppKey != "" {
		res.DingTalk.Enabled = true
		callbackURI := strings.TrimSuffix(frontendOrigin, "/") + "/#/loginCallback?provider=dingtalk"
		res.DingTalk.AuthURL = fmt.Sprintf("https://login.dingtalk.com/oauth2/auth?client_id=%s&response_type=code&scope=openid&redirect_uri=%s&state=dingtalk",
			integrateDing.AppKey, url.QueryEscape(callbackURI))
	}

	// OAuth2
	integrateOAuth := e.getIntegratedConfigRaw(gaia.SystemIntegrationOAuth2)
	if integrateOAuth.Status && integrateOAuth.AppID != "" && integrateOAuth.Config != "" {
		var configMap request.SystemOAuth2Request
		if err := json.Unmarshal([]byte(integrateOAuth.Config), &configMap); err != nil {
			return res
		}
		if configMap.ServerURL == "" || configMap.AuthorizeURL == "" {
			return res
		}
		res.OAuth2.Enabled = true
		redirectURI := strings.TrimSpace(configMap.RedirectUri)
		if redirectURI == "" {
			redirectURI = strings.TrimSuffix(frontendOrigin, "/") + "/#/loginCallback?provider=oauth2"
		}
		res.OAuth2.RedirectURI = redirectURI
		scope := strings.TrimSpace(configMap.Scope)
		if scope == "" {
			scope = "openid"
		}
		// Extend: 兼容 Casdoor 等 provider。用 net/url 解析并合并 query，保证 client_id 等参数一定被附加上去
		baseURLStr := strings.TrimSuffix(configMap.ServerURL, "/") + configMap.AuthorizeURL
		u, err := url.Parse(baseURLStr)
		if err != nil {
			// 解析失败时退回字符串拼接
			paramSep := "?"
			if strings.Contains(configMap.AuthorizeURL, "?") {
				paramSep = "&"
			}
			res.OAuth2.AuthURL = fmt.Sprintf("%s%sclient_id=%s&response_type=code&scope=%s&redirect_uri=%s&state=oauth2",
				baseURLStr, paramSep,
				url.QueryEscape(integrateOAuth.AppID), url.QueryEscape(scope), url.QueryEscape(redirectURI))
		} else {
			q := u.Query()
			q.Set("client_id", integrateOAuth.AppID)
			q.Set("response_type", "code")
			q.Set("scope", scope)
			q.Set("redirect_uri", redirectURI)
			q.Set("state", "oauth2")
			u.RawQuery = q.Encode()
			res.OAuth2.AuthURL = u.String()
		}
	}
	return res
}

// getIntegratedConfigRaw 获取集成配置（不脱敏，仅内部使用）
func (e *SystemIntegratedService) getIntegratedConfigRaw(classID uint) (integrate gaia.SystemIntegration) {
	if err := global.GVA_DB.Where("classify = ?", classID).First(&integrate).Error; err != nil {
		return gaia.SystemIntegration{Classify: classID, Status: false}
	}
	// 解密 AppSecret 供内部使用
	if secret, err := utils.DecryptBlowfish(integrate.AppSecret, global.GVA_CONFIG.JWT.SigningKey); err == nil {
		integrate.AppSecret = secret
	}
	return integrate
}
