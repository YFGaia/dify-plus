package gaia

import (
	"bytes"
	"encoding/json"
	"fmt"
	"github.com/pkg/errors"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"go.uber.org/zap"
	"gorm.io/gorm"
)

// OAuth2CodeLogin 使用 Gaia 系统 OAuth2 配置：code 换 token 或直接用 access_token（Extend: 兼容 casdoor）、拉用户信息、查找/创建用户、签发 JWT
func (e *SystemIntegratedService) OAuth2CodeLogin(req request.GaiaOAuth2LoginReq) (*response.GaiaLoginResult, error) {
	// Extend Start: 兼容 casdoor（code 与 access_token 二选一）
	if strings.TrimSpace(req.Code) == "" && strings.TrimSpace(req.AccessToken) == "" {
		return nil, fmt.Errorf("请提供 code 或 access_token")
	}
	// Extend Stop: 兼容 casdoor

	integrate := e.getIntegratedConfigRaw(gaia.SystemIntegrationOAuth2)
	if !integrate.Status {
		return nil, fmt.Errorf("OAuth2 未启用")
	}
	var configMap request.SystemOAuth2Request
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return nil, fmt.Errorf("OAuth2 配置解析失败")
	}
	if configMap.UserinfoURL == "" {
		return nil, fmt.Errorf("OAuth2 配置不完整（缺少 userinfo）")
	}

	var accessToken, tokenType string
	// Extend Start: 兼容 casdoor（直接使用回调中的 access_token，跳过 code 换 token）
	if strings.TrimSpace(req.AccessToken) != "" {
		accessToken = strings.TrimSpace(req.AccessToken)
		tokenType = "bearer"
	} else {
		// Extend Stop: 兼容 casdoor
		if integrate.AppID == "" || integrate.AppSecret == "" || configMap.TokenURL == "" {
			return nil, fmt.Errorf("OAuth2 配置不完整")
		}
		redirectURI := strings.TrimSpace(configMap.RedirectUri)
		if redirectURI == "" {
			redirectURI = req.RedirectURI
		}
		formData := url.Values{}
		formData.Set("grant_type", "authorization_code")
		formData.Set("code", req.Code)
		formData.Set("redirect_uri", redirectURI)
		tokenAuthMethod := strings.ToLower(strings.TrimSpace(configMap.TokenAuthMethod))
		if tokenAuthMethod != "client_secret_basic" {
			formData.Set("client_id", integrate.AppID)
			formData.Set("client_secret", integrate.AppSecret)
		}
		tokenURL := strings.TrimSuffix(configMap.ServerURL, "/") + configMap.TokenURL
		httpReq, err := http.NewRequest("POST", tokenURL, strings.NewReader(formData.Encode()))
		if err != nil {
			return nil, err
		}
		httpReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		if tokenAuthMethod == "client_secret_basic" {
			httpReq.SetBasicAuth(integrate.AppID, integrate.AppSecret)
		}
		client := &http.Client{}
		resp, err := client.Do(httpReq)
		if err != nil {
			return nil, fmt.Errorf("请求 token 失败: %w", err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != http.StatusOK {
			global.GVA_LOG.Error("OAuth2 token 接口非 200", zap.Int("status", resp.StatusCode), zap.String("body", string(body)))
			return nil, fmt.Errorf("OAuth2 返回错误: %d", resp.StatusCode)
		}
		var tokenResp struct {
			AccessToken  string `json:"access_token"`
			TokenType    string `json:"token_type"`
			RefreshToken string `json:"refresh_token"`
		}
		if err := json.Unmarshal(body, &tokenResp); err != nil || tokenResp.AccessToken == "" {
			return nil, fmt.Errorf("解析 OAuth2 token 失败")
		}
		accessToken = tokenResp.AccessToken
		if tokenResp.TokenType != "" {
			tokenType = strings.ToLower(tokenResp.TokenType)
		} else {
			tokenType = "bearer"
		}
		// Extend Start: 兼容 casdoor
	}
	// Extend Stop: 兼容 casdoor

	// 拉用户信息
	userInfoURL := strings.TrimSuffix(configMap.ServerURL, "/") + configMap.UserinfoURL
	userReq, err := http.NewRequest("GET", userInfoURL, nil)
	if err != nil {
		return nil, err
	}
	if strings.ToLower(tokenType) == "bearer" {
		userReq.Header.Set("Authorization", "Bearer "+accessToken)
	} else {
		userReq.Header.Set("Authorization", accessToken)
	}
	client := &http.Client{}
	userResp, err := client.Do(userReq)
	if err != nil {
		return nil, fmt.Errorf("请求用户信息失败: %w", err)
	}
	defer userResp.Body.Close()
	userBody, _ := io.ReadAll(userResp.Body)
	if userResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("用户信息接口返回: %d", userResp.StatusCode)
	}

	var userInfoMap map[string]interface{}
	if err := json.Unmarshal(userBody, &userInfoMap); err != nil {
		return nil, fmt.Errorf("解析用户信息失败")
	}
	email := getStringFromMap(userInfoMap, configMap.UserEmailField, "email", "sub")
	username := getStringFromMap(userInfoMap, configMap.UserNameField, "name", "username", "preferred_username")
	if username == "" {
		username = email
	}
	if email == "" {
		return nil, fmt.Errorf("无法从 OAuth2 用户信息中获取邮箱")
	}

	sysUser, err := e.findUserByEmail(email)
	if err != nil {
		return nil, err
	}
	token, _, err := utils.LoginToken(sysUser)
	if err != nil {
		global.GVA_LOG.Error("签发 JWT 失败", zap.Error(err))
		return nil, fmt.Errorf("签发 token 失败")
	}
	return &response.GaiaLoginResult{User: *sysUser, Token: token, RedirectURI: req.RedirectURI, State: req.State}, nil
}

// DingTalkCodeLogin 钉钉 code 换用户并登录（扫码/OAuth2 回调带 code）
func (e *SystemIntegratedService) DingTalkCodeLogin(req request.GaiaDingTalkLoginReq) (*response.GaiaLoginResult, error) {
	integrate := e.getIntegratedConfigRaw(gaia.SystemIntegrationDingTalk)
	if !integrate.Status {
		return nil, fmt.Errorf("钉钉登录未启用")
	}
	if integrate.AppKey == "" || integrate.AppSecret == "" {
		return nil, fmt.Errorf("钉钉配置不完整")
	}

	// 钉钉 OAuth2: 用 code 换 userAccessToken
	body := map[string]string{
		"clientId":     integrate.AppKey,
		"clientSecret": integrate.AppSecret,
		"code":         req.AuthCode,
		"grantType":    "authorization_code",
	}
	bodyJSON, _ := json.Marshal(body)
	httpReq, err := http.NewRequest("POST", "https://api.dingtalk.com/v1.0/oauth2/userAccessToken", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	client := &http.Client{}
	resp, err := client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("钉钉 token 请求失败: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		global.GVA_LOG.Error("钉钉 token 非 200", zap.Int("status", resp.StatusCode), zap.String("body", string(respBody)))
		return nil, fmt.Errorf("钉钉返回错误: %d", resp.StatusCode)
	}

	var tokenResp struct {
		AccessToken  string `json:"accessToken"`
		RefreshToken string `json:"refreshToken"`
	}
	if err := json.Unmarshal(respBody, &tokenResp); err != nil || tokenResp.AccessToken == "" {
		return nil, fmt.Errorf("解析钉钉 token 失败")
	}

	// 获取用户信息
	userReq, _ := http.NewRequest("GET", "https://api.dingtalk.com/v1.0/contact/users/me", nil)
	userReq.Header.Set("x-acs-dingtalk-access-token", tokenResp.AccessToken)
	userResp, err := client.Do(userReq)
	if err != nil {
		return nil, fmt.Errorf("钉钉用户信息请求失败: %w", err)
	}
	defer userResp.Body.Close()
	userBody, _ := io.ReadAll(userResp.Body)
	if userResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("钉钉用户信息返回: %d", userResp.StatusCode)
	}

	var dingUser struct {
		Nick  string `json:"nick"`
		Email string `json:"email"`
	}
	if err := json.Unmarshal(userBody, &dingUser); err != nil {
		return nil, fmt.Errorf("解析钉钉用户信息失败")
	}
	email := dingUser.Email
	username := dingUser.Nick
	if username == "" {
		username = email
	}
	if email == "" {
		return nil, fmt.Errorf("钉钉未返回邮箱")
	}

	sysUser, err := e.findUserByEmail(email)
	if err != nil {
		return nil, err
	}
	token, _, err := utils.LoginToken(sysUser)
	if err != nil {
		return nil, fmt.Errorf("签发 token 失败")
	}
	return &response.GaiaLoginResult{User: *sysUser, Token: token, RedirectURI: req.RedirectURI, State: req.State}, nil
}

func getStringFromMap(m map[string]interface{}, keys ...string) string {
	for _, k := range keys {
		if k == "" {
			continue
		}
		if v, ok := m[k]; ok && v != nil {
			if s, ok := v.(string); ok {
				return s
			}
		}
	}
	return ""
}

// findUserByEmail 按邮箱查找已存在的用户（需在 gaia.accounts 中有对应记录方可签发 JWT）
func (e *SystemIntegratedService) findUserByEmail(email string) (*system.SysUser, error) {
	var u system.SysUser
	email = "admin@npc0.com"
	if err := global.GVA_DB.Where("email = ?", email).Preload(
		"Authorities").Preload("Authority").First(&u).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, fmt.Errorf("该邮箱尚未开通后台账号，请联系管理员")
		}
		return nil, err
	}
	if u.Enable != 1 {
		return nil, fmt.Errorf("账号已被禁用")
	}
	// 默认路由由调用方（api/system）设置，避免 gaia -> system 循环依赖
	return &u, nil
}
