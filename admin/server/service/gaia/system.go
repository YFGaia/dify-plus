package gaia

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/faabiosr/cachego/file"
	"github.com/fastwego/dingding"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	gaiaResp "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
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
	// 1. 先直接调用钉钉 gettoken 接口，校验 AppKey/AppSecret 是否正确
	if strings.TrimSpace(req.AppKey) == "" || strings.TrimSpace(req.AppSecret) == "" {
		return nil, errors.New("AppKey 或 AppSecret 不能为空")
	}

	params := url.Values{}
	params.Add("appkey", req.AppKey)
	params.Add("appsecret", req.AppSecret)

	resp, err := http.Get("https://oapi.dingtalk.com/gettoken?" + params.Encode())
	if err != nil {
		return nil, fmt.Errorf("请求钉钉 gettoken 失败: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("钉钉 gettoken HTTP 状态异常: %d, body=%s", resp.StatusCode, string(body))
	}

	var tokenResp struct {
		ErrCode int    `json:"errcode"`
		ErrMsg  string `json:"errmsg"`
	}
	if err = json.Unmarshal(body, &tokenResp); err != nil {
		return nil, fmt.Errorf("解析钉钉 gettoken 响应失败: %w", err)
	}
	if tokenResp.ErrCode != 0 {
		return nil, fmt.Errorf("钉钉 gettoken 返回错误: errcode=%d, errmsg=%s", tokenResp.ErrCode, tokenResp.ErrMsg)
	}

	// 2. 校验通过后再构造 client（保持原有行为，供后续使用）
	var reqs *http.Request
	dingding.ServerUrl = "https://api.dingtalk.com"
	client := dingding.NewClient(&dingding.DefaultAccessTokenManager{
		Id:    uuid.New().String(),
		Cache: file.New(os.TempDir()),
		Name:  "x-acs-dingtalk-access-token",
		GetRefreshRequestFunc: func() *http.Request {
			// 这里沿用原来的 token 刷新逻辑
			reqs, _ = http.NewRequest(http.MethodGet, "https://oapi.dingtalk.com/gettoken?"+params.Encode(), nil)
			return reqs
		},
	})

	return client, nil
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
		return nil
	case gaia.SystemIntegrationOAuth2:
		// 测试OAuth2连接
		return e.TestOAuth2Connection(integrate, code)
	default:
		return errors.New("不支持的集成类型")
	}
}

// ParseDingTalkConfig 解析钉钉集成配置，自动处理新旧格式兼容
func (e *SystemIntegratedService) ParseDingTalkConfig(configJSON string) (request.DingTalkConfigRequest, error) {
	var configMap request.DingTalkConfigRequest
	if configJSON == "" {
		return configMap, nil
	}

	// 先解析顶层结构
	var raw struct {
		EmailApi      json.RawMessage       `json:"email_api"`
		ForwardConfig request.ForwardConfig `json:"forward_config"`
	}
	if err := json.Unmarshal([]byte(configJSON), &raw); err != nil {
		return configMap, fmt.Errorf("解析钉钉配置失败: %s", err.Error())
	}

	configMap.ForwardConfig = raw.ForwardConfig

	if raw.EmailApi != nil {
		cfg, err := parseEmailApiConfigFromJSON(raw.EmailApi)
		if err != nil {
			return configMap, err
		}
		configMap.EmailApi = cfg
	}

	return configMap, nil
}

// oldBodyDataCompat 旧格式 BodyData（兼容解析用）
type oldBodyDataCompat struct {
	FormData   []map[string]string `json:"form_data"`
	Urlencoded []map[string]string `json:"urlencoded"`
	Raw        string              `json:"raw"`
}

// isNewEmailApiConfig 检测配置是否使用新格式（通过 params 字段判断）
func isNewEmailApiConfig(config request.EmailApiConfig) bool {
	return config.Params != nil
}

// convertOldBodyDataToNew 将旧格式 BodyData（[]map[string]string）转换为新格式（[]BodyField）
func convertOldBodyDataToNew(old oldBodyDataCompat) request.BodyData {
	newData := request.BodyData{Raw: old.Raw}
	for _, kv := range old.FormData {
		for k, v := range kv {
			newData.FormData = append(newData.FormData, request.BodyField{
				Key:       k,
				ValueType: request.ValueTypeString,
				Value:     v,
			})
		}
	}
	for _, kv := range old.Urlencoded {
		for k, v := range kv {
			newData.Urlencoded = append(newData.Urlencoded, request.BodyField{
				Key:       k,
				ValueType: request.ValueTypeString,
				Value:     v,
			})
		}
	}
	return newData
}

// parseEmailApiConfigFromJSON 解析 EmailApiConfig，自动兼容新旧格式
// 新格式：包含 params 字段
// 旧格式：包含 request_param_field 字段，body_data 中使用 []map[string]string
func parseEmailApiConfigFromJSON(raw json.RawMessage) (request.EmailApiConfig, error) {
	// 先尝试解析为新格式
	var newConfig request.EmailApiConfig
	if err := json.Unmarshal(raw, &newConfig); err != nil {
		return request.EmailApiConfig{}, fmt.Errorf("解析邮箱配置失败: %s", err.Error())
	}

	// 如果有 params 字段，说明是新格式
	if isNewEmailApiConfig(newConfig) {
		return newConfig, nil
	}

	// 旧格式：尝试解析 body_data 中的 []map[string]string
	var oldCompat struct {
		request.EmailApiConfig
		BodyData oldBodyDataCompat `json:"body_data"`
	}
	if err := json.Unmarshal(raw, &oldCompat); err == nil {
		newConfig.BodyData = convertOldBodyDataToNew(oldCompat.BodyData)
		global.GVA_LOG.Info("邮箱配置：检测到旧格式，已在内存中转换为新格式",
			zap.String("request_param_field", newConfig.RequestParamField))
	}

	return newConfig, nil
}

// validateEmailApiConfigFields 验证 EmailApiConfig 字段
func validateEmailApiConfigFields(cfg request.EmailApiConfig) error {
	if !cfg.Enabled {
		return nil
	}
	if cfg.URL == "" {
		return errors.New("邮箱API URL不能为空")
	}
	if cfg.Method == "" {
		cfg.Method = "GET"
	}

	// 新格式不强制要求 request_param_field（通过 params 配置 URL 查询参数）
	if isNewEmailApiConfig(cfg) {
		// Params 只支持 string 和 ding_id 两种类型
		for i, p := range cfg.Params {
			if err := validateParamValueType(p.ValueType); err != nil {
				return fmt.Errorf("第%d个 URL 参数类型无效：%s", i+1, err.Error())
			}
		}
		// Body fields 支持 string、int、bool、ding_id
		for i, f := range cfg.BodyData.FormData {
			if err := validateValueType(f.ValueType); err != nil {
				return fmt.Errorf("form-data 第%d个字段类型无效：%s", i+1, err.Error())
			}
			if f.ValueType == request.ValueTypeInt && f.Value != "" {
				if _, err := strconv.ParseInt(f.Value, 10, 64); err != nil {
					return fmt.Errorf("form-data 第%d个字段（%s）的值不是有效整数：%s", i+1, f.Key, f.Value)
				}
			}
			if f.ValueType == request.ValueTypeBool && f.Value != "" {
				if _, err := strconv.ParseBool(f.Value); err != nil {
					return fmt.Errorf("form-data 第%d个字段（%s）的值不是有效布尔值：%s", i+1, f.Key, f.Value)
				}
			}
		}
		for i, f := range cfg.BodyData.Urlencoded {
			if err := validateValueType(f.ValueType); err != nil {
				return fmt.Errorf("urlencoded 第%d个字段类型无效：%s", i+1, err.Error())
			}
		}
	} else {
		// 旧格式兼容：request_param_field 不能为空
		if cfg.RequestParamField == "" {
			return errors.New("邮箱请求字段不能为空")
		}
	}

	if cfg.ResponseEmailField == "" {
		return errors.New("邮箱信息提取字段不能为空")
	}

	if cfg.Method != "GET" {
		bodyType := strings.ToLower(cfg.BodyType)
		if bodyType != "" && bodyType != "form-data" && bodyType != "x-www-form-urlencoded" && bodyType != "raw" {
			return fmt.Errorf("不支持的Body类型: %s，支持的类型: form-data, x-www-form-urlencoded, raw", cfg.BodyType)
		}
	}

	authType := strings.ToLower(cfg.Authorization.Type)
	if authType != "" && authType != "none" {
		if authType == "bearer" {
			if cfg.Authorization.Token == "" {
				return errors.New("Bearer Token不能为空")
			}
		} else if authType == "basic" {
			if cfg.Authorization.Username == "" || cfg.Authorization.Password == "" {
				return errors.New("Basic Auth需要填写Username和Password")
			}
		} else {
			return fmt.Errorf("不支持的Authorization类型: %s，支持的类型: none, bearer, basic", authType)
		}
	}
	return nil
}

// validateValueType 验证 Body 字段的 ValueType 是否合法（支持全部四种）
func validateValueType(vt string) error {
	switch vt {
	case "", request.ValueTypeString, request.ValueTypeInt, request.ValueTypeBool, request.ValueTypeDingID:
		return nil
	default:
		return fmt.Errorf("不支持的值类型: %s，支持的类型: string, int, bool, ding_id", vt)
	}
}

// validateParamValueType 验证 URL Params 的 ValueType 是否合法（只支持 string 和 ding_id）
func validateParamValueType(vt string) error {
	switch vt {
	case "", request.ValueTypeString, request.ValueTypeDingID:
		return nil
	default:
		return fmt.Errorf("URL 参数不支持的值类型: %s，支持的类型: string, ding_id", vt)
	}
}

// ValidateEmailApiConfig 验证第三方邮箱API配置
// @Tags System Integrated
// @Summary 验证第三方邮箱API配置
// @param: integrate gaia.SystemIntegration
// @return: error
func (e *SystemIntegratedService) ValidateEmailApiConfig(integrate gaia.SystemIntegration) error {
	if integrate.Config == "" {
		return nil
	}

	var configMap struct {
		EmailApi json.RawMessage `json:"email_api"`
	}
	if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
		return fmt.Errorf("解析配置失败: %s", err.Error())
	}
	if configMap.EmailApi == nil {
		return nil
	}

	cfg, err := parseEmailApiConfigFromJSON(configMap.EmailApi)
	if err != nil {
		return err
	}

	if err = validateEmailApiConfigFields(cfg); err != nil {
		return err
	}

	global.GVA_LOG.Info("第三方邮箱API配置验证通过",
		zap.String("url", cfg.URL),
		zap.String("method", cfg.Method),
		zap.String("body_type", cfg.BodyType),
		zap.String("auth_type", cfg.Authorization.Type),
		zap.Bool("new_format", isNewEmailApiConfig(cfg)))

	return nil
}

// TestEmailApiConfig 测试邮箱 API 配置，返回详细的响应结果用于调试
func (e *SystemIntegratedService) TestEmailApiConfig(cfg request.EmailApiConfig, testDingID string) (*gaiaResp.TestEmailApiConfigResponse, error) {
	if err := validateEmailApiConfigFields(cfg); err != nil {
		return &gaiaResp.TestEmailApiConfigResponse{
			IsValid:      false,
			ErrorMessage: "配置验证失败：" + err.Error(),
		}, nil
	}

	dingId := strings.TrimSpace(testDingID)
	if dingId == "" {
		return &gaiaResp.TestEmailApiConfigResponse{
			IsValid:      false,
			ErrorMessage: "测试钉钉 ID 不能为空，请先在弹窗中填写一个真实的 ding_id",
		}, nil
	}

	respBody, statusCode, reqErr := e.doEmailApiRequest(dingId, cfg)

	result := &gaiaResp.TestEmailApiConfigResponse{
		StatusCode: statusCode,
	}

	// 尝试解析响应 Body 为 JSON
	var bodyJSON interface{}
	if json.Unmarshal(respBody, &bodyJSON) == nil {
		result.Body = bodyJSON
	} else {
		result.Body = string(respBody)
	}

	if reqErr != nil {
		result.IsValid = false
		result.ErrorMessage = reqErr.Error()
		return result, nil
	}

	// 尝试提取邮箱字段
	if cfg.ResponseEmailField != "" {
		if bodyMap, ok := bodyJSON.(map[string]interface{}); ok {
			email := extractJSONPathAdvanced(bodyMap, cfg.ResponseEmailField)
			if email != "" {
				result.EmailFieldPreview = cfg.ResponseEmailField + " = " + email
				result.IsValid = true
			} else {
				result.IsValid = false
				result.ErrorMessage = "未找到邮箱字段：" + cfg.ResponseEmailField
			}
		} else if bodySlice, ok := bodyJSON.([]interface{}); ok {
			email := extractJSONPathAdvanced(bodySlice, cfg.ResponseEmailField)
			if email != "" {
				result.EmailFieldPreview = cfg.ResponseEmailField + " = " + email
				result.IsValid = true
			} else {
				result.IsValid = false
				result.ErrorMessage = "未找到邮箱字段：" + cfg.ResponseEmailField
			}
		} else {
			result.IsValid = statusCode >= 200 && statusCode < 300
		}
	} else {
		result.IsValid = statusCode >= 200 && statusCode < 300
	}

	global.GVA_LOG.Info("测试邮箱 API 配置",
		zap.Int("status_code", statusCode),
		zap.String("email_preview", result.EmailFieldPreview),
		zap.Bool("is_valid", result.IsValid))

	return result, nil
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

	// 若未配置转发 Token，则认为未使用转发能力，不强制校验
	if len(configMap.ForwardConfig.Tokens) == 0 {
		return nil
	}

	// 使用转发能力的前置条件：至少 1 个 Token + 启用并配置「第三方邮箱配置」
	if !configMap.EmailApi.Enabled || strings.TrimSpace(configMap.EmailApi.URL) == "" {
		return errors.New("使用转发能力前请先启用并配置「第三方邮箱配置」")
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
		zap.Int("token_count", len(configMap.ForwardConfig.Tokens)))

	return nil
}

// ValidateDingIdApiConfig 验证第三方钉钉 ID 匹配 API 配置
// @Tags System Integrated
// @Summary 验证第三方钉钉 ID 匹配 API 配置
// @param: integrate gaia.SystemIntegration
// @return: error
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

// resolveParamValue 根据 ValueType 解析参数值，ding_id 类型替换为实际的钉钉 ID
func resolveParamValue(vt, value, dingId string) string {
	switch vt {
	case request.ValueTypeDingID:
		return dingId
	default:
		// 兼容旧格式的 {{ding_id}} 占位符和新格式的 $<{[ding_id]}> 标记
		v := strings.ReplaceAll(value, "{{ding_id}}", dingId)
		v = strings.ReplaceAll(v, request.DingIDMarker, dingId)
		return v
	}
}

// buildBodyFields 将 []BodyField 按类型转换，构建 url.Values（用于 form-data 和 urlencoded）
func buildBodyFields(fields []request.BodyField, dingId string) url.Values {
	form := url.Values{}
	for _, f := range fields {
		if f.Key == "" {
			continue
		}
		val := resolveParamValue(f.ValueType, f.Value, dingId)
		form.Set(f.Key, val)
	}
	return form
}

// buildURL 根据新格式 Params 或旧格式 RequestParamField 构建带查询参数的 URL
func buildURL(baseURL string, config request.EmailApiConfig, dingId string) string {
	if isNewEmailApiConfig(config) {
		// 新格式：遍历 Params 列表自动拼接
		params := url.Values{}
		for _, p := range config.Params {
			if p.Key == "" {
				continue
			}
			params.Set(p.Key, resolveParamValue(p.ValueType, p.Value, dingId))
		}
		if len(params) == 0 {
			return baseURL
		}
		sep := "?"
		if strings.Contains(baseURL, "?") {
			sep = "&"
		}
		return baseURL + sep + params.Encode()
	}

	// 旧格式：RequestParamField 字段名 + dingId 作为值
	if config.RequestParamField != "" {
		sep := "?"
		if strings.Contains(baseURL, "?") {
			sep = "&"
		}
		return baseURL + sep + url.QueryEscape(config.RequestParamField) + "=" + url.QueryEscape(dingId)
	}
	return baseURL
}

// callEmailApi 调用第三方邮箱 API，使用 ding_id(用户名) 获取邮箱
func (e *SystemIntegratedService) callEmailApi(
	dingId string, config request.EmailApiConfig) (mailList []string, err error) {
	// init
	respBody, _, err := e.doEmailApiRequest(dingId, config)
	if err != nil {
		return mailList, err
	}

	var respJSON map[string]interface{}
	if err = json.Unmarshal(respBody, &respJSON); err != nil {
		return mailList, fmt.Errorf("解析响应 JSON 失败：%s", err.Error())
	}

	email := extractJSONPathAdvanced(respJSON, config.ResponseEmailField)
	if email == "" {
		return mailList, fmt.Errorf("响应中未找到邮箱（路径：%s）", config.ResponseEmailField)
	}
	//
	mailList = append(mailList, email)
	parts := strings.Split(email, "@")
	defaultMail := os.Getenv(gaia.EmailDomainEnv)
	if len(defaultMail) > 1 && len(parts) > 1 && len(parts[0]) > 0 {
		mailList = append(mailList, parts[0]+"@"+defaultMail)
	}

	return mailList, nil
}

// doEmailApiRequest 构建并执行邮箱 API 请求，返回响应体字节和状态码
func (e *SystemIntegratedService) doEmailApiRequest(dingId string, config request.EmailApiConfig) ([]byte, int, error) {
	method := strings.ToUpper(config.Method)
	if method == "" {
		method = "GET"
	}

	var bodyReader io.Reader
	var contentType string
	if method != "GET" && method != "HEAD" {
		switch strings.ToLower(config.BodyType) {
		case "raw":
			raw := strings.ReplaceAll(config.BodyData.Raw, "{{ding_id}}", dingId)
			raw = strings.ReplaceAll(raw, request.DingIDMarker, dingId)
			bodyReader = strings.NewReader(raw)
			contentType = "application/json"
		case "form-data":
			form := buildBodyFields(config.BodyData.FormData, dingId)
			// 也处理旧格式的 urlencoded 字段（兼容）
			if len(form) == 0 {
				form = buildBodyFields(config.BodyData.Urlencoded, dingId)
			}
			bodyReader = strings.NewReader(form.Encode())
			contentType = "multipart/form-data"
		case "x-www-form-urlencoded":
			form := buildBodyFields(config.BodyData.Urlencoded, dingId)
			if len(form) == 0 {
				form = buildBodyFields(config.BodyData.FormData, dingId)
			}
			bodyReader = strings.NewReader(form.Encode())
			contentType = "application/x-www-form-urlencoded"
		}
	}

	apiURL := buildURL(config.URL, config, dingId)

	req, err := http.NewRequest(method, apiURL, bodyReader)
	if err != nil {
		return nil, 0, fmt.Errorf("构建请求失败：%s", err.Error())
	}

	// 设置 Content-Type（如果 Headers 未覆盖）
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}

	// 设置 Headers（可覆盖 Content-Type）
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

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("请求失败：%s", err.Error())
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("读取响应失败：%s", err.Error())
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return respBody, resp.StatusCode, fmt.Errorf("第三方 API 返回错误状态码：%d", resp.StatusCode)
	}

	return respBody, resp.StatusCode, nil
}

// extractJSONPathAdvanced 支持点分路径和数组索引（如 data[0].userName）
func extractJSONPathAdvanced(data interface{}, path string) string {
	if path == "" {
		return ""
	}
	parts := splitJSONPath(path)
	current := data
	for _, part := range parts {
		switch v := current.(type) {
		case map[string]interface{}:
			current = v[part.key]
		case []interface{}:
			if part.isIndex && part.index >= 0 && part.index < len(v) {
				current = v[part.index]
			} else {
				return ""
			}
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
		return strconv.FormatFloat(v, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(v)
	default:
		return fmt.Sprintf("%v", v)
	}
}

type jsonPathPart struct {
	key     string
	isIndex bool
	index   int
}

// splitJSONPath 将路径字符串分割为结构化的部分列表，支持 data[0].userName 格式
func splitJSONPath(path string) []jsonPathPart {
	var parts []jsonPathPart
	// 先按点分割
	segments := strings.Split(path, ".")
	for _, seg := range segments {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		// 检查是否包含数组索引 data[0]
		if idx := strings.Index(seg, "["); idx != -1 {
			key := seg[:idx]
			rest := seg[idx:]
			if key != "" {
				parts = append(parts, jsonPathPart{key: key})
			}
			// 解析所有 [N] 部分
			for len(rest) > 0 && rest[0] == '[' {
				end := strings.Index(rest, "]")
				if end == -1 {
					break
				}
				idxStr := rest[1:end]
				rest = rest[end+1:]
				if n, err := strconv.Atoi(idxStr); err == nil {
					parts = append(parts, jsonPathPart{isIndex: true, index: n})
				}
			}
		} else {
			parts = append(parts, jsonPathPart{key: seg})
		}
	}
	return parts
}

// ParseForwardToken 从 Bearer Token 中验签并提取 ding_id
// 遍历 tokens 列表，找到签名匹配的条目
func (e *SystemIntegratedService) ParseForwardToken(
	rawToken string, tokens []request.ForwardToken) (dingId string, err error) {
	parts := strings.SplitN(rawToken, ".", 2)
	if len(parts) != 2 {
		return "", errors.New("token 格式非法")
	}
	payloadB64, sigB64 := parts[0], parts[1]
	sigBytes, err := base64.RawURLEncoding.DecodeString(sigB64)
	if err != nil {
		return "", errors.New("签名解码失败")
	}
	for _, t := range tokens {
		if t.TokenSecret == "" {
			continue
		}
		// 直接使用原始字节作为 HMAC 密钥，兼容任意字符串格式的密钥
		secret := []byte(t.TokenSecret)
		// 验证 HMAC 签名
		mac := hmac.New(sha256.New, secret)
		mac.Write([]byte(payloadB64))
		expected := mac.Sum(nil)
		if !hmac.Equal(expected, sigBytes) {
			continue // 不匹配，试下一个
		}
		// 签名验证通过，解析 payload
		payloadBytes, err := base64.RawURLEncoding.DecodeString(payloadB64)
		if err != nil {
			return "", errors.New("payload 解码失败")
		}
		var payload struct {
			DingID string `json:"ding_id"`
		}
		if err := json.Unmarshal(payloadBytes, &payload); err != nil {
			return "", errors.New("payload 解析失败")
		}
		if payload.DingID == "" {
			return "", errors.New("token 中缺少 ding_id")
		}
		return payload.DingID, nil
	}
	return "", errors.New("无效的转发 Token")
}

// ResolveAccountByDingId 通过钉钉 ID 解析 gaia account_id
// 解析顺序：Redis 缓存 → AccountDingTalkExtend 本地表 → 第三方 EmailApi（邮箱 API）
func (e *SystemIntegratedService) ResolveAccountByDingId(
	dingId string, apiConfig request.EmailApiConfig) (string, error) {

	// 1. 查 Redis 缓存
	ctx := context.Background()
	redisKey := gaia.RedisKeyGaiaForwardDingPrefix + dingId
	if cached, err := global.GVA_REDIS.Get(ctx, redisKey).Result(); err == nil && cached != "" {
		return cached, nil
	}

	// 2. 查本地 AccountDingTalkExtend 表
	var extend gaia.AccountDingTalkExtend
	if err := global.GVA_DB.Where("ding_talk = ?", dingId).First(&extend).Error; err == nil {
		accountID := extend.ID.String()
		global.GVA_REDIS.Set(ctx, redisKey, accountID, 24*time.Hour)
		return accountID, nil
	}

	// 3. 第三方邮箱 API（若配置）
	if !apiConfig.Enabled || strings.TrimSpace(apiConfig.URL) == "" {
		return "", fmt.Errorf("未找到 ding_id=%s 对应的用户，且未配置第三方邮箱 API", dingId)
	}

	email, err := e.callEmailApi(dingId, apiConfig)
	if err != nil {
		return "", fmt.Errorf("调用第三方邮箱 API 失败：%s", err.Error())
	}

	// 4. 按邮箱查 accounts 表（匹配 email 字段）
	var account gaia.Account
	if err = global.GVA_DB.Where("email = ?", email).First(&account).Error; err != nil {
		return "", fmt.Errorf("用户 %s 不存在（来自第三方邮箱 API）", email)
	}

	accountID := account.ID.String()

	// 5. 写回 AccountDingTalkExtend，方便下次本地命中
	global.GVA_DB.Create(&gaia.AccountDingTalkExtend{
		ID:       account.ID,
		DingTalk: dingId,
	})

	// 6. 写 Redis 缓存
	global.GVA_REDIS.Set(ctx, redisKey, accountID, 24*time.Hour)
	return accountID, nil
}
