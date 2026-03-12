package gaia

import (
	"bytes"
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/pkg/errors"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// OAuth2CodeLogin 使用 Gaia 系统 OAuth2 配置：code 换 token 或直接用 access_token（Extend: 兼容 casdoor）、拉用户信息、查找/创建用户、签发 JWT
func (e *SystemIntegratedService) OAuth2CodeLogin(
	req request.GaiaOAuth2LoginReq) (*response.GaiaLoginResult, error) {
	// init
	var accessToken, tokenType string
	var configMap request.SystemOAuth2Request
	if strings.TrimSpace(req.Code) == "" && strings.TrimSpace(req.AccessToken) == "" {
		return nil, fmt.Errorf("请提供 code 或 access_token")
	}

	integrate := e.getIntegratedConfigRaw(gaia.SystemIntegrationOAuth2)
	if !integrate.Status {
		return nil, fmt.Errorf("OAuth2 未启用")
	}
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return nil, fmt.Errorf("OAuth2 配置解析失败")
	}
	if configMap.UserinfoURL == "" {
		return nil, fmt.Errorf("OAuth2 配置不完整（缺少 userinfo）")
	}

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
		formData.Set("code", req.Code)
		formData.Set("redirect_uri", redirectURI)
		formData.Set("grant_type", "authorization_code")
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
		var tokenResp map[string]interface{}
		if err = json.Unmarshal(body, &tokenResp); err != nil || tokenResp["access_token"] == "" {
			return nil, fmt.Errorf("解析 OAuth2 token 失败")
		}
		accessToken = tokenResp["access_token"].(string)
		if tokenCache, ok := tokenResp["token_type"]; ok {
			tokenType = strings.ToLower(tokenCache.(string))
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
	if err = json.Unmarshal(userBody, &userInfoMap); err != nil {
		return nil, fmt.Errorf("解析用户信息失败")
	}
	// 用户信息映射：支持 Jinja 风格路径，如 name、email、phone 或 user.name、data.attributes.phone
	username := getStringByPathOrKeys(userInfoMap, configMap.UserNameField, "name", "username", "preferred_username")
	email := getStringByPathOrKeys(userInfoMap, configMap.UserEmailField, "email", "sub")
	userID := getStringByPathOrKeys(userInfoMap, configMap.UserIDField, "phone", "sub", "id")
	if username == "" {
		username = email
		if username == "" {
			username = userID
		}
	}
	if email == "" && userID == "" {
		return nil, fmt.Errorf("无法从 OAuth2 用户信息中获取邮箱或用户唯一标识")
	}

	fmt.Println("OAuth2CodeLogin", email, username)
	sysUser, err := e.findUserByEmailOrPhone(email, userID)
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

// DingTalkTestCallback 仅用 code 换 token，用于「测试连接」回调，不登录、不写 session
func (e *SystemIntegratedService) DingTalkTestCallback(code string) error {
	code = strings.TrimSpace(code)
	if code == "" {
		return fmt.Errorf("授权码为空")
	}
	integrate := e.getIntegratedConfigRaw(gaia.SystemIntegrationDingTalk)
	if integrate.AppKey == "" || integrate.AppSecret == "" {
		return fmt.Errorf("钉钉配置不完整")
	}
	bodyJSON, _ := json.Marshal(map[string]string{
		"clientId":     integrate.AppKey,
		"clientSecret": integrate.AppSecret,
		"code":         code,
		"grantType":    "authorization_code",
	})
	httpReq, err := http.NewRequest("POST", "https://api.dingtalk.com/v1.0/oauth2/userAccessToken", bytes.NewReader(bodyJSON))
	if err != nil {
		return err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	client := &http.Client{}
	resp, err := client.Do(httpReq)
	if err != nil {
		return fmt.Errorf("钉钉 token 请求失败: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		global.GVA_LOG.Error("钉钉 token 非 200", zap.Int("status", resp.StatusCode), zap.String("body", string(respBody)))
		return fmt.Errorf("钉钉返回错误: %d", resp.StatusCode)
	}
	var tokenResp map[string]interface{}
	if err = json.Unmarshal(respBody, &tokenResp); err != nil || tokenResp["accessToken"] == "" {
		return fmt.Errorf("解析钉钉 token 失败")
	}
	return nil
}

// DingTalkCodeLogin 钉钉 code 换用户并登录（扫码/OAuth2 回调带 code）
func (e *SystemIntegratedService) DingTalkCodeLogin(
	req request.GaiaDingTalkLoginReq) (*response.GaiaLoginResult, error) {
	integrate := e.getIntegratedConfigRaw(gaia.SystemIntegrationDingTalk)
	if !integrate.Status {
		return nil, fmt.Errorf("钉钉登录未启用")
	}
	if integrate.AppKey == "" || integrate.AppSecret == "" {
		return nil, fmt.Errorf("钉钉配置不完整")
	}

	// 钉钉 OAuth2: 用 code 换 userAccessToken
	bodyJSON, _ := json.Marshal(map[string]string{
		"clientId":     integrate.AppKey,
		"clientSecret": integrate.AppSecret,
		"code":         req.AuthCode,
		"grantType":    "authorization_code",
	})
	httpReq, err := http.NewRequest("POST",
		"https://api.dingtalk.com/v1.0/oauth2/userAccessToken", bytes.NewReader(bodyJSON))
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
		global.GVA_LOG.Error("钉钉 token 非 200", zap.Int(
			"status", resp.StatusCode), zap.String("body", string(respBody)))
		return nil, fmt.Errorf("钉钉返回错误: %d", resp.StatusCode)
	}

	var tokenResp map[string]interface{}
	if err = json.Unmarshal(respBody, &tokenResp); err != nil || tokenResp["accessToken"] == "" {
		return nil, fmt.Errorf("解析钉钉 token 失败")
	}

	// 获取用户信息
	userReq, _ := http.NewRequest("GET", "https://api.dingtalk.com/v1.0/contact/users/me", nil)
	userReq.Header.Set("x-acs-dingtalk-access-token", tokenResp["accessToken"].(string))
	userResp, err := client.Do(userReq)
	if err != nil {
		return nil, fmt.Errorf("钉钉用户信息请求失败: %w", err)
	}
	defer userResp.Body.Close()
	userBody, _ := io.ReadAll(userResp.Body)
	if userResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("钉钉用户信息返回: %d", userResp.StatusCode)
	}

	var dingUser map[string]interface{}
	if err = json.Unmarshal(userBody, &dingUser); err != nil {
		return nil, fmt.Errorf("解析钉钉用户信息失败")
	}

	// 提取钉钉 ID（user_id 字段）
	dingId := ""
	if v, ok := dingUser["unionId"]; ok && v != nil {
		dingId, _ = v.(string)
	}
	if dingId == "" {
		if v, ok := dingUser["userId"]; ok && v != nil {
			dingId, _ = v.(string)
		}
	}

	// 解析用户名配置
	var emailList []string
	var configMap request.DingTalkConfigRequest
	var emailConfig request.EmailApiConfig
	if integrate.Config != "" {
		if jsonErr := json.Unmarshal([]byte(integrate.Config), &configMap); jsonErr == nil {
			var rawMsg json.RawMessage
			if rawBytes, marshalErr := json.Marshal(configMap.EmailApi); marshalErr == nil {
				rawMsg = rawBytes
				if cfg, parseErr := parseEmailApiConfigFromJSON(rawMsg); parseErr == nil {
					emailConfig = cfg
				}
			}
		}
	}

	// 优先通过用户名 API 获取用户名（新格式）
	if emailConfig.Enabled && dingId != "" {
		emailList, err = e.callEmailApi(dingId, emailConfig)
		if err == nil && len(emailList) > 0 {
			fmt.Println("钉钉 code 换用户并登录（扫码/OAuth2 回调带 code）", emailList)
			sysUser, findErr := e.findUserByEmail(emailList)
			if findErr != nil {
				return nil, findErr
			}
			token, _, tokenErr := utils.LoginToken(sysUser)
			if tokenErr != nil {
				return nil, fmt.Errorf("签发 token 失败")
			}
			return &response.GaiaLoginResult{User: *sysUser, Token: token, RedirectURI: req.RedirectURI, State: req.State}, nil
		}
		global.GVA_LOG.Warn("DingTalkCodeLogin: 第三方邮箱 API 获取失败，尝试钉钉直接返回邮箱",
			zap.String("ding_id", dingId), zap.Error(err))
	}

	// 回退：直接从钉钉用户信息获取邮箱
	email, _ := dingUser["email"].(string)
	username, _ := dingUser["nick"].(string)
	if username == "" {
		username = email
	}
	if email == "" {
		return nil, fmt.Errorf("钉钉未返回邮箱")
	}

	fmt.Println("钉钉 code 换用户并登录第三方邮箱 API 获取失败", email)
	sysUser, err := e.findUserByEmail([]string{email})
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
			var s string
			if s, ok = v.(string); ok {
				return s
			}
		}
	}
	return ""
}

// getStringByJinjaPath 按 Jinja 风格路径从 map 提取字符串，支持点分路径如 "name"、"user.email"、"data.attributes.phone"；
// path 可带 {{ }}，会先去除再解析。
func getStringByJinjaPath(m map[string]interface{}, path string) string {
	path = strings.TrimSpace(path)
	path = strings.TrimPrefix(path, "{{")
	path = strings.TrimSuffix(path, "}}")
	path = strings.TrimSpace(path)
	if path == "" {
		return ""
	}
	keys := strings.Split(path, ".")
	var current interface{} = m
	for _, key := range keys {
		key = strings.TrimSpace(key)
		if key == "" {
			continue
		}
		switch v := current.(type) {
		case map[string]interface{}:
			current = v[key]
		case map[string]string:
			current = v[key]
		default:
			return ""
		}
		if current == nil {
			return ""
		}
	}
	switch v := current.(type) {
	case string:
		return v
	case float64:
		return fmt.Sprintf("%v", v)
	case int:
		return fmt.Sprintf("%d", v)
	case int64:
		return fmt.Sprintf("%d", v)
	case bool:
		if v {
			return "true"
		}
		return "false"
	default:
		return fmt.Sprintf("%v", current)
	}
}

// getStringByPathOrKeys 优先用 Jinja 路径从 m 取值，若为空则按 keys 顺序从顶层取。
func getStringByPathOrKeys(m map[string]interface{}, path string, fallbackKeys ...string) string {
	if path != "" {
		if s := getStringByJinjaPath(m, path); s != "" {
			return s
		}
	}
	return getStringFromMap(m, fallbackKeys...)
}

// findUserByEmail 按username查找已存在的用户（需在 gaia.accounts 中有对应记录方可签发 JWT）
func (e *SystemIntegratedService) findUserByEmail(mailList []string) (*system.SysUser, error) {
	// 查询关联邮箱
	var u system.SysUser
	if err := global.GVA_DB.Where("email IN (?)", mailList).Preload(
		"Authorities").Preload("Authority").First(&u).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, fmt.Errorf("%s尚未开通账号，请联系管理员", mailList[0])
		}
		return nil, err
	}
	if u.Enable != 1 {
		return nil, fmt.Errorf("账号已被禁用")
	}
	// 默认路由由调用方（api/system）设置，避免 gaia -> system 循环依赖
	return &u, nil
}

// findUserByEmailOrPhone 按邮箱或用户唯一标识（如手机号）查找用户，优先邮箱
func (e *SystemIntegratedService) findUserByEmailOrPhone(
	mail, userID string) (u *system.SysUser, err error) {
	if mail != "" {
		if u, err = e.findUserByEmail([]string{mail}); err == nil {
			return u, nil
		}
		// 仅当“未开通”时再尝试按 userID(phone) 查，其他错误直接返回
		if !strings.Contains(err.Error(), "尚未开通") {
			return nil, err
		}
	}
	if userID != "" {
		if err = global.GVA_DB.Where("phone = ?", userID).Preload(
			"Authorities").Preload("Authority").First(&u).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return nil, fmt.Errorf("该用户唯一标识尚未开通后台账号，请联系管理员")
			}
			return nil, err
		}
		if u.Enable != 1 {
			return nil, fmt.Errorf("账号已被禁用")
		}
		return u, nil
	}
	return nil, fmt.Errorf("无法从 OAuth2 用户信息中获取邮箱或用户唯一标识")
}
