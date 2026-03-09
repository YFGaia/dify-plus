package gaia

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/faabiosr/cachego/file"
	"github.com/fastwego/dingding"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

type SystemIntegratedService struct{}

// GetIntegratedConfig
// @Tags System Integrated
// @Summary 获取系统集成配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
func (e *SystemIntegratedService) GetIntegratedConfig(classID uint) (integrate gaia.SystemIntegration) {
	// classID是否在
	var err error
	if err = global.GVA_DB.Where("classify = ?", classID).First(&integrate).Error; err != nil {
		integrate = gaia.SystemIntegration{
			Classify: classID,
			Status:   false,
		}
		// 创建相关集成信息
		global.GVA_DB.Create(&integrate)
	}
	// 隐藏部分加密信息
	var secret string
	if secret, err = utils.DecryptBlowfish(integrate.AppSecret, global.GVA_CONFIG.JWT.SigningKey); err == nil {
		integrate.AppSecret = utils.AddAsteriskToString(secret)
	}
	integrate.CorpID = utils.AddAsteriskToString(integrate.CorpID)
	return integrate
}

// SetIntegratedConfig
// @Tags System Integrated
// @Summary 设置系统集成配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @param: integrate gaia.SystemIntegration, code string, test bool
// @return: err error
func (e *SystemIntegratedService) SetIntegratedConfig(
	integrate gaia.SystemIntegration, code string, test bool) (err error) {
	// classID是否在
	var log gaia.SystemIntegration
	if err = global.GVA_DB.Where("classify = ?", integrate.Classify).First(&log).Error; err != nil {
		return err
	}
	// AppSecret
	var secret string
	if secret, err = utils.DecryptBlowfish(log.AppSecret, global.GVA_CONFIG.JWT.SigningKey); err == nil {
		encodeSecret := utils.AddAsteriskToString(secret)
		if encodeSecret != integrate.AppSecret {
			if secret, err = utils.EncryptBlowfish(
				[]byte(integrate.AppSecret), global.GVA_CONFIG.JWT.SigningKey); err != nil {
				return errors.New("AppSecret加密失败")
			}
			// save
			log.AppSecret = secret
		} else {
			// 为什么不用 integrate.AppSecret, 被加*了
			if secret, err = utils.DecryptBlowfish(log.AppSecret, global.GVA_CONFIG.JWT.SigningKey); err != nil {
				return errors.New("AppSecret解析失败")
			}
			integrate.AppSecret = secret
		}
	}
	// CorpID
	if utils.AddAsteriskToString(log.CorpID) != integrate.CorpID {
		log.CorpID = integrate.CorpID
	}
	// AppID 不加密，直接赋值
	log.AppID = integrate.AppID
	// 关闭不需要请求
	if integrate.Status || test {
		// 测试连接
		if err = e.TestConnection(integrate, code); err != nil {
			return errors.New("连接失败:" + err.Error())
		}
	}
	// Test completed
	if test {
		return err
	}
	// save
	if err = global.GVA_DB.Model(&gaia.SystemIntegration{}).Where(
		"id=?", log.Id).Updates(&map[string]interface{}{
		"config":     integrate.Config,
		"status":     integrate.Status,
		"agent_id":   integrate.AgentID,
		"app_key":    integrate.AppKey,
		"app_secret": log.AppSecret,
		"corp_id":    log.CorpID,
		"app_id":     log.AppID,
	}).Error; err != nil {
		return err
	}
	return nil
}

// DingTalkConfigAvailable
// @Tags System Integrated
// @Summary 测试钉钉配置是否可用
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @param: req gaia.SystemIntegration
// @return: *dingding.Client, error
func (e *SystemIntegratedService) DingTalkConfigAvailable(req gaia.SystemIntegration) (*dingding.Client, error) {
	var err error
	var reqs *http.Request
	dingding.ServerUrl = "https://api.dingtalk.com"
	// 特殊需要，检查可用性就不设置缓存了
	return dingding.NewClient(&dingding.DefaultAccessTokenManager{
		Id:    uuid.New().String(),
		Cache: file.New(os.TempDir()),
		Name:  "x-acs-dingtalk-access-token",
		GetRefreshRequestFunc: func() *http.Request {
			params := url.Values{}
			params.Add("appkey", req.AppKey)
			params.Add("appsecret", req.AppSecret)
			reqs, err = http.NewRequest(http.MethodGet, "https://oapi.dingtalk.com/gettoken?"+params.Encode(), nil)
			return reqs
		},
	}), err
}

// TestConnection 测试连接
// @Tags System Integrated
// @Summary 测试系统集成连接
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @param: integrate gaia.SystemIntegration, code string
// @return: error
func (e *SystemIntegratedService) TestConnection(integrate gaia.SystemIntegration, code string) error {
	switch integrate.Classify {
	case gaia.SystemIntegrationDingTalk:
		// 测试钉钉连接
		if _, err := e.DingTalkConfigAvailable(integrate); err != nil {
			return errors.New("钉钉链接失败: " + err.Error())
		}
		// 验证第三方邮箱API配置
		if err := e.ValidateEmailApiConfig(integrate); err != nil {
			global.GVA_LOG.Warn("第三方邮箱API配置验证失败", zap.Error(err))
			// 不阻止保存，只记录警告
		}
		// 验证转发集成配置
		if err := e.ValidateForwardConfig(integrate); err != nil {
			global.GVA_LOG.Warn("转发集成配置验证失败", zap.Error(err))
			// 不阻止保存，只记录警告
		}
		// 验证第三方钉钉 ID 匹配 API 配置
		if err := e.ValidateDingIdApiConfig(integrate); err != nil {
			global.GVA_LOG.Warn("第三方钉钉 ID 匹配 API 配置验证失败", zap.Error(err))
			// 不阻止保存，只记录警告
		}
		return nil
	case gaia.SystemIntegrationOAuth2:
		// 测试OAuth2连接
		return e.TestOAuth2Connection(integrate, code)
	default:
		return errors.New("不支持的集成类型")
	}
}

// ValidateEmailApiConfig 验证第三方邮箱API配置
// @Tags System Integrated
// @Summary 验证第三方邮箱API配置
// @param: integrate gaia.SystemIntegration
// @return: error
func (e *SystemIntegratedService) ValidateEmailApiConfig(integrate gaia.SystemIntegration) error {
	// 解析Config字段
	if integrate.Config == "" {
		return nil // 配置为空不算错误
	}

	var configMap request.DingTalkConfigRequest
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return fmt.Errorf("解析配置失败: %s", err.Error())
	}

	// 检查是否启用邮箱API
	if !configMap.EmailApi.Enabled {
		return nil // 未启用不需要验证
	}

	// 验证必填字段
	if configMap.EmailApi.URL == "" {
		return errors.New("邮箱API URL不能为空")
	}

	if configMap.EmailApi.Method == "" {
		configMap.EmailApi.Method = "GET"
	}

	if configMap.EmailApi.RequestParamField == "" {
		return errors.New("邮箱请求字段不能为空")
	}

	if configMap.EmailApi.ResponseEmailField == "" {
		return errors.New("邮箱信息提取字段不能为空")
	}

	// 验证Body类型（仅POST/PUT/DELETE需要）
	if configMap.EmailApi.Method != "GET" {
		bodyType := strings.ToLower(configMap.EmailApi.BodyType)
		if bodyType == "" {
			configMap.EmailApi.BodyType = "raw" // 默认raw
		} else if bodyType != "form-data" && bodyType != "x-www-form-urlencoded" && bodyType != "raw" {
			return fmt.Errorf("不支持的Body类型: %s，支持的类型: form-data, x-www-form-urlencoded, raw", bodyType)
		}
	}

	// 验证Authorization配置
	authType := strings.ToLower(configMap.EmailApi.Authorization.Type)
	if authType != "" && authType != "none" {
		if authType == "bearer" {
			if configMap.EmailApi.Authorization.Token == "" {
				return errors.New("Bearer Token不能为空")
			}
		} else if authType == "basic" {
			if configMap.EmailApi.Authorization.Username == "" || configMap.EmailApi.Authorization.Password == "" {
				return errors.New("Basic Auth需要填写Username和Password")
			}
		} else {
			return fmt.Errorf("不支持的Authorization类型: %s，支持的类型: none, bearer, basic", authType)
		}
	}

	global.GVA_LOG.Info("第三方邮箱API配置验证通过",
		zap.String("url", configMap.EmailApi.URL),
		zap.String("method", configMap.EmailApi.Method),
		zap.String("body_type", configMap.EmailApi.BodyType),
		zap.String("auth_type", configMap.EmailApi.Authorization.Type))

	return nil
}

// TestOAuth2Connection 测试OAuth2连接
// @Tags System Integrated
// @Summary 测试OAuth2连接
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @param: integrate gaia.SystemIntegration, code string
// @return: error
func (e *SystemIntegratedService) TestOAuth2Connection(integrate gaia.SystemIntegration, code string) (err error) {
	// 解析Config字段
	var configMap request.SystemOAuth2Request
	if err = json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		global.GVA_LOG.Error("解析OAuth2配置失败!", zap.Error(err))
		return err
	}
	// 没有code的（保存操作）
	if len(code) == 0 {
		return nil
	}
	// 检查必要字段
	if configMap.ServerURL == "" || configMap.TokenURL == "" || integrate.AppID == "" || integrate.AppSecret == "" {
		return errors.New("请填写完整的 OAuth2 配置信息")
	}

	// 合成请求byte
	formData := url.Values{}
	formData.Set("grant_type", "authorization_code")
	formData.Set("code", code)
	// redirect_uri 必须与授权时一致
	formData.Set("redirect_uri", strings.TrimSpace(configMap.RedirectUri))
	// 支持basic与post两种认证方式
	// 默认使用client_secret_post，除非配置为basic
	tokenAuthMethod := strings.ToLower(strings.TrimSpace(configMap.TokenAuthMethod))
	useBasic := tokenAuthMethod == "client_secret_basic"
	if !useBasic {
		formData.Set("client_secret", integrate.AppSecret)
		formData.Set("client_id", integrate.AppID)
	}

	// 发送请求
	var req *http.Request
	client := &http.Client{}
	req, err = http.NewRequest("POST", fmt.Sprintf(
		"%s%s", configMap.ServerURL, configMap.TokenURL), strings.NewReader(formData.Encode()))
	if err != nil {
		global.GVA_LOG.Error("创建测试请求失败", zap.Error(err))
		return errors.New(fmt.Sprintf("创建测试请求失败: %s", err.Error()))
	}

	// 设置认证与Content-Type
	if useBasic {
		req.SetBasicAuth(integrate.AppID, integrate.AppSecret)
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	} else {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}

	// 发送请求
	resp, err := client.Do(req)
	if err != nil {
		global.GVA_LOG.Error("测试 OAuth2 连接失败", zap.Error(err))
		return errors.New(fmt.Sprintf("连接 OAuth2 服务器失败: %s", err.Error()))
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		global.GVA_LOG.Error("测试 OAuth2 连接失败", zap.Int("status", resp.StatusCode))
		return errors.New(fmt.Sprintf("OAuth2 服务器返回错误状态码: %d", resp.StatusCode))
	}
	var bodyByte []byte
	if bodyByte, err = io.ReadAll(resp.Body); err != nil {
		return fmt.Errorf("OAuth2 request io.ReadAll: %s", resp.Status)
	}

	var tokenMap request.SystemOAuth2Error
	if err = json.Unmarshal(bodyByte, &tokenMap); err == nil && tokenMap.Code != 0 {
		return fmt.Errorf("OAuth2 Eroor: %s", tokenMap.Info)
	}

	return nil
}

// ValidateForwardConfig 验证转发集成配置
// @Tags System Integrated
// @Summary 验证转发集成配置
// @param: integrate gaia.SystemIntegration
// @return: error
func (e *SystemIntegratedService) ValidateForwardConfig(integrate gaia.SystemIntegration) error {
	// 解析 Config 字段
	if integrate.Config == "" {
		return nil // 配置为空不算错误
	}

	var configMap request.DingTalkConfigRequest
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return fmt.Errorf("解析配置失败：%s", err.Error())
	}

	// 检查是否启用转发
	if !configMap.ForwardConfig.Enabled {
		return nil // 未启用不需要验证
	}

	// 验证 Token 数量
	if len(configMap.ForwardConfig.Tokens) > 20 {
		return errors.New("转发 Token 最多 20 个")
	}

	// 验证每个 Token 的必填字段
	for i, token := range configMap.ForwardConfig.Tokens {
		if token.ID == "" {
			return fmt.Errorf("第%d个 Token 的 ID 不能为空", i+1)
		}
		if token.TokenHash == "" {
			return fmt.Errorf("第%d个 Token 的 TokenHash 不能为空", i+1)
		}
	}

	global.GVA_LOG.Info("转发集成配置验证通过",
		zap.Bool("enabled", configMap.ForwardConfig.Enabled),
		zap.Int("token_count", len(configMap.ForwardConfig.Tokens)))

	return nil
}

// ValidateDingIdApiConfig 验证第三方钉钉 ID 匹配 API 配置
// @Tags System Integrated
// @Summary 验证第三方钉钉 ID 匹配 API 配置
// @param: integrate gaia.SystemIntegration
// @return: error
func (e *SystemIntegratedService) ValidateDingIdApiConfig(integrate gaia.SystemIntegration) error {
	// 解析 Config 字段
	if integrate.Config == "" {
		return nil // 配置为空不算错误
	}

	var configMap request.DingTalkConfigRequest
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return fmt.Errorf("解析配置失败：%s", err.Error())
	}

	// 检查是否启用 DingIdApi
	if !configMap.ForwardConfig.DingIdApi.Enabled {
		return nil // 未启用不需要验证
	}

	// 验证必填字段
	if configMap.ForwardConfig.DingIdApi.URL == "" {
		return errors.New("钉钉 ID 匹配 API URL 不能为空")
	}

	if configMap.ForwardConfig.DingIdApi.Method == "" {
		configMap.ForwardConfig.DingIdApi.Method = "GET"
	}

	if configMap.ForwardConfig.DingIdApi.RequestParamField == "" {
		return errors.New("钉钉 ID 请求字段不能为空")
	}

	if configMap.ForwardConfig.DingIdApi.ResponseUserNamePath == "" {
		return errors.New("响应用户名路径不能为空")
	}

	// 验证 Body 类型（仅 POST/PUT/DELETE 需要）
	if configMap.ForwardConfig.DingIdApi.Method != "GET" && configMap.ForwardConfig.DingIdApi.Method != "HEAD" {
		bodyType := strings.ToLower(configMap.ForwardConfig.DingIdApi.BodyType)
		if bodyType == "" {
			configMap.ForwardConfig.DingIdApi.BodyType = "raw" // 默认 raw
		} else if bodyType != "form-data" && bodyType != "x-www-form-urlencoded" && bodyType != "raw" {
			return fmt.Errorf("不支持的 Body 类型：%s，支持的类型：form-data, x-www-form-urlencoded, raw", bodyType)
		}
	}

	// 验证 Authorization 配置
	authType := strings.ToLower(configMap.ForwardConfig.DingIdApi.Authorization.Type)
	if authType != "" && authType != "none" {
		if authType == "bearer" {
			if configMap.ForwardConfig.DingIdApi.Authorization.Token == "" {
				return errors.New("Bearer Token 不能为空")
			}
		} else if authType == "basic" {
			if configMap.ForwardConfig.DingIdApi.Authorization.Username == "" || configMap.ForwardConfig.DingIdApi.Authorization.Password == "" {
				return errors.New("Basic Auth 需要填写 Username 和 Password")
			}
		} else {
			return fmt.Errorf("不支持的 Authorization 类型：%s，支持的类型：none, bearer, basic", authType)
		}
	}

	global.GVA_LOG.Info("第三方钉钉 ID 匹配 API 配置验证通过",
		zap.String("url", configMap.ForwardConfig.DingIdApi.URL),
		zap.String("method", configMap.ForwardConfig.DingIdApi.Method),
		zap.String("body_type", configMap.ForwardConfig.DingIdApi.BodyType),
		zap.String("auth_type", configMap.ForwardConfig.DingIdApi.Authorization.Type))

	return nil
}

// extractJSONPath 按点分路径从 JSON 对象中提取字符串值，支持 "data.username" 等多层路径
func extractJSONPath(data map[string]interface{}, path string) string {
	parts := strings.SplitN(path, ".", 2)
	val, ok := data[parts[0]]
	if !ok {
		return ""
	}
	if len(parts) == 1 {
		if s, ok := val.(string); ok {
			return s
		}
		return fmt.Sprintf("%v", val)
	}
	if nested, ok := val.(map[string]interface{}); ok {
		return extractJSONPath(nested, parts[1])
	}
	return ""
}

// callDingIdApi 调用第三方钉钉 ID 匹配 API，返回 username
func (e *SystemIntegratedService) callDingIdApi(dingId string, config request.DingIdApiConfig) (string, error) {
	method := strings.ToUpper(config.Method)
	if method == "" {
		method = "GET"
	}

	var bodyReader io.Reader
	if method != "GET" && method != "HEAD" {
		switch strings.ToLower(config.BodyType) {
		case "raw":
			// 替换 Body 中的 {{ding_id}} 占位符
			raw := strings.ReplaceAll(config.BodyData.Raw, "{{ding_id}}", dingId)
			bodyReader = strings.NewReader(raw)
		case "form-data", "x-www-form-urlencoded":
			form := url.Values{}
			for _, kv := range config.BodyData.Urlencoded {
				for k, v := range kv {
					form.Set(k, strings.ReplaceAll(v, "{{ding_id}}", dingId))
				}
			}
			if config.BodyType == "form-data" {
				for _, kv := range config.BodyData.FormData {
					for k, v := range kv {
						form.Set(k, strings.ReplaceAll(v, "{{ding_id}}", dingId))
					}
				}
			}
			bodyReader = strings.NewReader(form.Encode())
		}
	}

	// 构建请求 URL（GET 时把 ding_id 拼入 query）
	apiURL := config.URL
	if method == "GET" || method == "HEAD" {
		if config.RequestParamField != "" {
			sep := "?"
			if strings.Contains(apiURL, "?") {
				sep = "&"
			}
			apiURL = apiURL + sep + url.QueryEscape(config.RequestParamField) + "=" + url.QueryEscape(dingId)
		}
	}

	req, err := http.NewRequest(method, apiURL, bodyReader)
	if err != nil {
		return "", fmt.Errorf("构建请求失败：%s", err.Error())
	}

	// 设置 Headers
	for k, v := range config.Headers {
		req.Header.Set(k, v)
	}

	// 设置 Authorization
	authType := strings.ToLower(config.Authorization.Type)
	switch authType {
	case "bearer":
		req.Header.Set("Authorization", "Bearer "+config.Authorization.Token)
	case "basic":
		req.SetBasicAuth(config.Authorization.Username, config.Authorization.Password)
	}

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("请求失败：%s", err.Error())
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("第三方 API 返回错误状态码：%d", resp.StatusCode)
	}

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("读取响应失败：%s", err.Error())
	}

	var respJSON map[string]interface{}
	if err = json.Unmarshal(respBody, &respJSON); err != nil {
		return "", fmt.Errorf("解析响应 JSON 失败：%s", err.Error())
	}

	userName := extractJSONPath(respJSON, config.ResponseUserNamePath)
	if userName == "" {
		return "", fmt.Errorf("响应中未找到用户名（路径：%s）", config.ResponseUserNamePath)
	}

	return userName, nil
}

// ResolveAccountByDingId 通过钉钉 ID 解析 gaia account_id
// 解析顺序：Redis 缓存 → AccountDingTalkExtend 本地表 → 第三方 DingIdApi
func (e *SystemIntegratedService) ResolveAccountByDingId(dingId string, apiConfig request.DingIdApiConfig) (string, error) {
	ctx := context.Background()
	redisKey := "gaia:forward:ding:" + dingId

	// 1. 查 Redis 缓存
	if cached, err := global.GVA_REDIS.Get(ctx, redisKey).Result(); err == nil && cached != "" {
		global.GVA_LOG.Info("ResolveAccountByDingId: Redis 命中", zap.String("ding_id", dingId), zap.String("account_id", cached))
		return cached, nil
	}

	// 2. 查本地 AccountDingTalkExtend 表
	var extend gaia.AccountDingTalkExtend
	if err := global.GVA_DB.Where("ding_talk = ?", dingId).First(&extend).Error; err == nil {
		accountID := extend.ID.String()
		global.GVA_LOG.Info("ResolveAccountByDingId: 本地表命中", zap.String("ding_id", dingId), zap.String("account_id", accountID))
		global.GVA_REDIS.Set(ctx, redisKey, accountID, 24*time.Hour)
		return accountID, nil
	}

	// 3. 第三方 DingIdApi（若配置且启用）
	if !apiConfig.Enabled || apiConfig.URL == "" {
		return "", fmt.Errorf("未找到 ding_id=%s 对应的用户，且未配置第三方 API", dingId)
	}

	userName, err := e.callDingIdApi(dingId, apiConfig)
	if err != nil {
		return "", fmt.Errorf("调用第三方 DingId API 失败：%s", err.Error())
	}

	// 4. 按 username 查 accounts 表（匹配 name 字段）
	var account gaia.Account
	if err = global.GVA_DB.Where("name = ?", userName).First(&account).Error; err != nil {
		return "", fmt.Errorf("用户名 %s 不存在（来自第三方 API）", userName)
	}

	accountID := account.ID.String()

	// 5. 写回 AccountDingTalkExtend，方便下次本地命中
	global.GVA_DB.Create(&gaia.AccountDingTalkExtend{
		ID:       account.ID,
		DingTalk: dingId,
	})

	// 6. 写 Redis 缓存
	global.GVA_REDIS.Set(ctx, redisKey, accountID, 24*time.Hour)

	global.GVA_LOG.Info("ResolveAccountByDingId: 第三方 API 解析成功",
		zap.String("ding_id", dingId),
		zap.String("username", userName),
		zap.String("account_id", accountID))

	return accountID, nil
}
