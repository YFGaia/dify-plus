package gaia

import (
	"bufio"
	"bytes"
	"context"
	"crypto/aes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha1"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	gaiaRequest "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	gaiaResponse "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"go.gnd.pw/crypto/eax"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// ModelProviderService 模型提供商服务，负责提供商配置、凭证获取、可用模型拉取及聊天请求代理。
type ModelProviderService struct{}

// GetProviderList 获取提供商配置列表
// @Tags System Integrated
// @Summary 获取提供商配置列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
//
// 只展示三种逻辑提供商：openai（OpenAI）、tongyi（千问/通义）、google（Google）。
// Dify 里插件名为 langgenius/openai/openai、langgenius/tongyi/tongyi 等，与上述一一对应，不单独成行。
// 匹配规则：
//   - 列表项 provider_name 固定为短名：openai / tongyi / google
//   - 启用/已选模型：来自 admin 表 model_provider_config，按短名存储（provider_name = openai 等）
//   - 可用模型：通过各提供商官方 API 拉取（OpenAI/通义兼容 GET /v1/models），不再使用 Dify provider_models
//   - 凭证：来自 Dify providers + provider_credentials，按候选名查（见 difyProviderNameCandidates）
func (s *ModelProviderService) GetProviderList() ([]gaiaResponse.ProviderListItem, error) {
	var configs []gaia.ModelProviderConfig
	if err := global.GVA_DB.Find(&configs).Error; err != nil {
		return nil, err
	}

	// 只展示三种逻辑提供商；langgenius/openai/openai 等视为 openai 的数据来源，不单独列出
	result := make([]gaiaResponse.ProviderListItem, len(gaia.SupportedProviders))
	for i, providerName := range gaia.SupportedProviders {
		var config *gaia.ModelProviderConfig
		for j := range configs {
			if configs[j].ProviderName == providerName {
				config = &configs[j]
				break
			}
		}

		item := gaiaResponse.ProviderListItem{
			ProviderName:    providerName,
			Enabled:         false,
			Models:          []string{},
			AvailableModels: []gaiaResponse.ModelInfo{},
		}
		if config != nil {
			item.Enabled = config.Enabled
			if config.Models != "" {
				json.Unmarshal([]byte(config.Models), &item.Models)
			}
		}
		result[i] = item
	}

	// 异步并发拉取各提供商的可用模型
	var wg sync.WaitGroup
	for i, providerName := range gaia.SupportedProviders {
		wg.Add(1)
		go func(idx int, name string) {
			defer wg.Done()
			availableModels, err := s.GetAvailableModelsFromDify(name)
			if err != nil {
				global.GVA_LOG.Warn("获取提供商可用模型失败", zap.String("provider", name), zap.Error(err))
			} else {
				result[idx].AvailableModels = availableModels
			}
		}(i, providerName)
	}
	wg.Wait()

	return result, nil
}

// UpdateProviderConfig 更新指定提供商的启用状态及已选模型列表。
// @Tags System Integrated
// @Summary 更新提供商配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
//
// 参数:
//   - providerName: 提供商短名（openai/tongyi/google）
//   - enabled: 是否启用
//   - models: 已选模型 ID 列表
func (s *ModelProviderService) UpdateProviderConfig(providerName string, enabled bool, models []string) error {
	modelsJSON, err := json.Marshal(models)
	if err != nil {
		return err
	}

	var config gaia.ModelProviderConfig
	err = global.GVA_DB.Where("provider_name = ?", providerName).First(&config).Error

	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			// 创建新记录
			config = gaia.ModelProviderConfig{
				ProviderName: providerName,
				Enabled:      enabled,
				Models:       string(modelsJSON),
			}
			return global.GVA_DB.Create(&config).Error
		}
		return err
	}

	// 更新现有记录
	config.Enabled = enabled
	config.Models = string(modelsJSON)
	return global.GVA_DB.Save(&config).Error
}

// GetEnabledModels 获取所有已启用提供商的已选模型，以 OpenAI /v1/models 响应格式返回。
// @Tags System Integrated
// @Summary 获取已启用的模型列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
func (s *ModelProviderService) GetEnabledModels() (gaiaResponse.OpenAIModelsResponse, error) {
	var configs []gaia.ModelProviderConfig
	if err := global.GVA_DB.Where("enabled = ?", true).Find(&configs).Error; err != nil {
		return gaiaResponse.OpenAIModelsResponse{}, err
	}

	resp := gaiaResponse.OpenAIModelsResponse{
		Data: []gaiaResponse.ModelInfo{},
	}

	for _, config := range configs {
		var models []string
		if config.Models != "" {
			if err := json.Unmarshal([]byte(config.Models), &models); err != nil {
				continue
			}
		}

		for _, modelID := range models {
			resp.Data = append(resp.Data, gaiaResponse.ModelInfo{
				ID:   modelID,
				Name: modelID,
			})
		}
	}

	return resp, nil
}

// GetAvailableModelsFromDify 通过各提供商官方 API 拉取可用模型列表（不使用 Dify provider_models 表）。
// @Tags System Integrated
// @Summary 获取提供商的可用模型列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
//
// 参数 providerName 为短名（openai/tongyi/google）。未配置凭证时返回空列表且不报错。
func (s *ModelProviderService) GetAvailableModelsFromDify(providerName string) ([]gaiaResponse.ModelInfo, error) {
	creds, err := s.GetDifyProviderCredentials(providerName)
	if err != nil || creds.APIKey == "" {
		return nil, nil // 未配置凭证时返回空列表，不报错
	}

	client := &http.Client{Timeout: 15 * time.Second}
	switch providerName {
	case gaia.ProviderOpenai:
		base := creds.Endpoint
		if base == "" {
			base = "https://api.openai.com"
		}
		return s.fetchOpenAICompatibleModels(client, base, creds.APIKey)
	case gaia.ProviderTongyi:
		// 通义兼容 OpenAI 接口：GET .../v1/models
		return s.fetchOpenAICompatibleModels(
			client, "https://dashscope.aliyuncs.com/api", creds.APIKey)
	case gaia.ProviderGoogle:
		// Google Gemini: GET https://generativelanguage.googleapis.com/v1beta/models?key=API_KEY
		base := creds.Endpoint
		if base == "" {
			base = gaia.DefaultAPIBase[gaia.ProviderGoogle]
		}
		return s.fetchGeminiModels(client, base, creds.APIKey)
	case gaia.ProviderAnthropic:
		// Anthropic 使用 /v1/messages，模型列表接口不同，暂返回空
		return nil, nil
	default:
		if creds.Endpoint != "" {
			return s.fetchOpenAICompatibleModels(client, creds.Endpoint, creds.APIKey)
		}
		return nil, nil
	}
}

// fetchOpenAICompatibleModels 调用 OpenAI 兼容的 GET /v1/models，解析为 ModelInfo 列表。
// 兼容两种响应格式：
// 1) OpenAI: { "data": [ { "id": "..." }, ... ] }
// 2) 通义: { "success": true, "output": { "models": [ { "model": "...", "name": "..." }, ... ] } }
func (s *ModelProviderService) fetchOpenAICompatibleModels(client *http.Client, baseURL, apiKey string) ([]gaiaResponse.ModelInfo, error) {
	url := strings.TrimSuffix(baseURL, "/") + "/v1/models"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		global.GVA_LOG.Warn("拉取模型列表接口非 200", zap.String("url", url), zap.Int("status", resp.StatusCode), zap.String("body", string(body)))
		return nil, fmt.Errorf("接口返回 %d", resp.StatusCode)
	}

	// 先尝试 OpenAI 格式
	var listResp gaiaResponse.OpenAIModelsListResponse
	if err = json.Unmarshal(body, &listResp); err == nil && len(listResp.Data) > 0 {
		list := make([]gaiaResponse.ModelInfo, 0, len(listResp.Data))
		for _, m := range listResp.Data {
			if m.ID != "" {
				list = append(list, gaiaResponse.ModelInfo{ID: m.ID, Name: m.ID})
			}
		}
		return list, nil
	}

	// 再尝试通义格式：success + output.models
	var tongyiResp gaiaResponse.TongyiModelsListResponse
	if err = json.Unmarshal(body, &tongyiResp); err != nil {
		return nil, fmt.Errorf("解析模型列表失败（非 OpenAI 也非通义格式）: %w", err)
	}
	if !tongyiResp.Success || len(tongyiResp.Output.Models) == 0 {
		return nil, fmt.Errorf("通义接口返回无模型或 success 不为 true")
	}
	list := make([]gaiaResponse.ModelInfo, 0, len(tongyiResp.Output.Models))
	for _, m := range tongyiResp.Output.Models {
		if m.Model != "" {
			name := m.Name
			if name == "" {
				name = m.Model
			}
			list = append(list, gaiaResponse.ModelInfo{ID: m.Model, Name: name})
		}
	}
	return list, nil
}

// fetchGeminiModels 调用 Google Gemini GET /v1beta/models?key=API_KEY，解析 models[]，支持分页。
// 认证使用 query 参数 key，响应格式：{ "models": [ { "name": "models/xxx", "baseModelId": "xxx", "displayName": "..." } ], "nextPageToken": "..." }
func (s *ModelProviderService) fetchGeminiModels(client *http.Client, baseURL, apiKey string) ([]gaiaResponse.ModelInfo, error) {
	baseURL = strings.TrimSuffix(baseURL, "/")
	all := make([]gaiaResponse.ModelInfo, 0)
	pageToken := ""

	for {
		url := baseURL + "/v1beta/models?key=" + apiKey
		if pageToken != "" {
			url += "&pageToken=" + pageToken
		}
		req, err := http.NewRequest("GET", url, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			global.GVA_LOG.Warn("拉取 Gemini 模型列表非 200", zap.String("url", baseURL+"/v1beta/models"), zap.Int("status", resp.StatusCode), zap.String("body", string(body)))
			return nil, fmt.Errorf("接口返回 %d", resp.StatusCode)
		}

		var listResp gaiaResponse.GeminiModelsListResponse
		if err = json.Unmarshal(body, &listResp); err != nil {
			return nil, fmt.Errorf("解析 Gemini 模型列表失败: %w", err)
		}

		for _, m := range listResp.Models {
			// 请求时使用 baseModelId（如 gemini-1.5-flash），无则用 name 去掉 "models/" 前缀
			id := m.BaseModelID
			if id == "" && m.Name != "" {
				id = strings.TrimPrefix(m.Name, "models/")
			}
			if id == "" {
				continue
			}
			name := m.DisplayName
			if name == "" {
				name = id
			}
			all = append(all, gaiaResponse.ModelInfo{ID: id, Name: name})
		}

		pageToken = listResp.NextPageToken
		if pageToken == "" {
			break
		}
	}

	return all, nil
}

// GetDifyProviderCredentials 从 Dify 数据库（providers + provider_credentials）读取指定提供商的凭证，支持缓存与解密。
// @Tags System Integrated
// @Summary 获取提供商凭证
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
func (s *ModelProviderService) GetDifyProviderCredentials(providerName string) (
	creds *gaiaResponse.ProviderCredentials, err error) {
	creds = &gaiaResponse.ProviderCredentials{}

	// 首先尝试从Redis缓存获取（按请求的 providerName 缓存）
	var cached string
	cacheKey := fmt.Sprintf("model_provider_credentials:%s", providerName)
	if cached, err = global.GVA_Dify_REDIS.Get(context.Background(), cacheKey).Result(); err == nil {
		if err = json.Unmarshal([]byte(cached), &creds); err == nil {
			return creds, nil
		}
	}

	// 从数据库查询，同时获取 tenant_id
	var row gaia.ProviderCredential
	if err = global.GVA_DB.Table("providers").
		Select("provider_credentials.encrypted_config, providers.tenant_id").
		Joins("LEFT JOIN provider_credentials ON providers.credential_id = provider_credentials.id").
		Where("providers.provider_name LIKE ? AND providers.provider_type = ? AND providers.is_valid = ?",
			fmt.Sprintf("%%%s%%", providerName), gaia.DifyProviderTypeCustom, true).
		First(&row).Error; err != nil {
		return creds, fmt.Errorf("未找到提供商 %s 的凭证配置", providerName)
	}

	// 兼容两种存储：1) 明文 JSON（如 {"openai_api_key":"...", "openai_api_base":"..."}）；2) Dify RSA+AES-EAX 加密后再 base64
	var base string
	var configMap map[string]interface{}
	if err = json.Unmarshal([]byte(row.EncryptedConfig), &configMap); err == nil {
		// 解密函数用于处理加密的值
		if config, ok := configMap[gaia.ConfigKeyOpenaiAPIKey]; ok {
			creds.APIKey, err = s.decryptConfig(config.(string), row.TenantID)
			if base, ok = configMap[gaia.ConfigKeyOpenaiAPIBase].(string); ok && strings.TrimSpace(base) != "" {
				creds.Endpoint = strings.TrimSuffix(strings.TrimSpace(base), "/")
			}
		} else if config, ok = configMap[gaia.ConfigKeyDashScopeAPIKey]; ok {
			creds.APIKey, err = s.decryptConfig(config.(string), row.TenantID)
		} else if config, ok = configMap[gaia.ConfigKeyAPIKey]; ok {
			creds.APIKey, err = s.decryptConfig(config.(string), row.TenantID)
		} else {
			// 尝试从备选字段中查找
			for _, key := range gaia.CredentialKeyFallback {
				var v string
				if v, ok = configMap[key].(string); ok && v != "" {
					if creds.APIKey, err = s.decryptConfig(v, row.TenantID); err == nil && creds.APIKey != "" {
						break
					}
				}
			}
			if base, ok = configMap[gaia.ConfigKeyOpenaiAPIBase].(string); ok && strings.TrimSpace(base) != "" {
				creds.Endpoint = strings.TrimSuffix(strings.TrimSpace(base), "/")
			}
		}
		if err != nil {
			return nil, fmt.Errorf("解密凭证失败: %w", err)
		}
	}
	if creds.APIKey == "" {
		return nil, fmt.Errorf("未能从配置中提取API Key")
	}

	// 缓存凭证（1小时）
	var cacheJSON []byte
	if cacheJSON, err = json.Marshal(creds); err == nil {
		global.GVA_Dify_REDIS.Set(context.Background(), cacheKey, cacheJSON, time.Hour)
	}

	return creds, nil
}

// decryptConfig 解密Dify的加密配置（RSA + AES-EAX 混合加密）
// Dify 使用 RSA 2048 + AES-EAX 混合加密，密文格式为：
// Base64( "HYBRID:" + enc_aes_key(256字节) + nonce(16字节) + tag(16字节) + ciphertext )
func (s *ModelProviderService) decryptConfig(encryptedConfig string, tenantID string) (string, error) {
	// 1. Base64 解码
	encrypted, err := base64.StdEncoding.DecodeString(encryptedConfig)
	if err != nil {
		return "", fmt.Errorf("base64 decode failed: %w", err)
	}

	// 2. 检查并去除 "HYBRID:" 前缀
	prefix := []byte("HYBRID:")
	if !bytes.HasPrefix(encrypted, prefix) {
		// 如果没有 HYBRID 前缀，可能是明文或其他格式，直接返回原值
		return encryptedConfig, nil
	}
	encrypted = encrypted[len(prefix):]

	// 3. 读取 tenant 私钥
	privateKey, err := s.loadPrivateKey(tenantID)
	if err != nil {
		return "", fmt.Errorf("load private key failed: %w", err)
	}

	// 4. 解析密文结构
	// RSA 2048 = 256 字节密钥
	rsaKeySize := privateKey.Size() // 通常是 256
	if len(encrypted) < rsaKeySize+32 {
		return "", errors.New("encrypted data too short")
	}

	encAESKey := encrypted[:rsaKeySize]
	nonce := encrypted[rsaKeySize : rsaKeySize+16]
	tag := encrypted[rsaKeySize+16 : rsaKeySize+32]
	ciphertext := encrypted[rsaKeySize+32:]

	// 5. RSA OAEP 解密 AES 密钥（使用 SHA-1，与 Dify Python 实现一致）
	aesKey, err := rsa.DecryptOAEP(sha1.New(), rand.Reader, privateKey, encAESKey, nil)
	if err != nil {
		return "", fmt.Errorf("RSA decrypt failed: %w", err)
	}

	// 6. AES-EAX 解密数据
	plaintext, err := s.aesEAXDecrypt(aesKey, nonce, ciphertext, tag)
	if err != nil {
		return "", fmt.Errorf("AES-EAX decrypt failed: %w", err)
	}

	return string(plaintext), nil
}

// loadPrivateKey 从配置的存储路径加载指定 tenant 的 RSA 私钥（PEM 文件）。
func (s *ModelProviderService) loadPrivateKey(tenantID string) (*rsa.PrivateKey, error) {
	// 私钥路径: {storage-path}/privkeys/{tenant_id}/private.pem
	// 可通过配置自定义存储路径
	storagePath := global.GVA_CONFIG.Gaia.StoragePath
	if storagePath == "" {
		// 默认路径：Docker 环境使用 /app/storage，本地开发使用相对路径
		storagePath = "/app/storage"
	}

	filepath := fmt.Sprintf("%s/privkeys/%s/private.pem", storagePath, tenantID)

	// 如果默认路径不存在，尝试本地开发相对路径
	if _, err := os.Stat(filepath); os.IsNotExist(err) && storagePath == "/app/storage" {
		// 本地开发环境：admin/server 相对于 api/storage
		localPath := fmt.Sprintf("../../api/storage/privkeys/%s/private.pem", tenantID)
		if _, err := os.Stat(localPath); err == nil {
			filepath = localPath
		}
	}

	pemData, err := os.ReadFile(filepath)
	if err != nil {
		return nil, fmt.Errorf("read private key file failed: %w", err)
	}

	// 解析 PEM 格式私钥
	block, _ := pem.Decode(pemData)
	if block == nil {
		return nil, errors.New("failed to decode PEM block")
	}

	// 尝试解析 PKCS#1 格式
	privateKey, err := x509.ParsePKCS1PrivateKey(block.Bytes)
	if err != nil {
		// 尝试解析 PKCS#8 格式
		key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse private key failed: %w", err)
		}
		var ok bool
		privateKey, ok = key.(*rsa.PrivateKey)
		if !ok {
			return nil, errors.New("private key is not RSA key")
		}
	}

	return privateKey, nil
}

// aesEAXDecrypt 使用 AES-EAX 解密数据
// EAX 模式是一种认证加密模式，使用第三方库 go.gnd.pw/crypto/eax 实现
func (s *ModelProviderService) aesEAXDecrypt(key, nonce, ciphertext, tag []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}

	// 创建 EAX AEAD 实例
	aead, err := eax.NewEAX(block)
	if err != nil {
		return nil, fmt.Errorf("create EAX cipher failed: %w", err)
	}

	// EAX 的 Open 方法需要 nonce 和 ciphertext+tag 的组合
	// Python pycryptodome 的格式: ciphertext 和 tag 是分开的
	// Go EAX 库的 Open 期望格式: ciphertext || tag
	combined := make([]byte, len(ciphertext)+len(tag))
	copy(combined, ciphertext)
	copy(combined[len(ciphertext):], tag)

	// 解密并验证
	plaintext, err := aead.Open(nil, nonce, combined, nil)
	if err != nil {
		return nil, fmt.Errorf("EAX decrypt failed: %w", err)
	}

	return plaintext, nil
}

// ProxyChat 将聊天请求代理到上游提供商，校验模型已开启并写入流式/非流式响应到 writer，并记录代理日志。
// @Tags System Integrated
// @Summary 代理聊天请求
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
func (s *ModelProviderService) ProxyChat(userID string, req gaiaRequest.ChatRequest, writer io.Writer) error {
	// 检查模型是否开启
	providerName, err := s.getProviderByModel(req.Model)
	if err != nil {
		return err
	}

	// 验证模型是否在开启列表中
	if !s.isModelEnabled(providerName, req.Model) {
		return fmt.Errorf("模型 %s 未开启", req.Model)
	}

	// 获取提供商凭证
	creds, err := s.GetDifyProviderCredentials(providerName)
	if err != nil {
		return err
	}

	// 获取上游端点
	endpoint := s.getUpstreamEndpoint(providerName)

	// 构建请求
	reqBody, err := json.Marshal(req)
	if err != nil {
		return err
	}

	httpReq, err := http.NewRequest("POST", endpoint, bytes.NewReader(reqBody))
	if err != nil {
		return err
	}

	// 设置请求头
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", fmt.Sprintf("Bearer %s", creds.APIKey))

	// 发送请求
	client := &http.Client{
		Timeout: 5 * time.Minute,
	}

	resp, err := client.Do(httpReq)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("上游返回错误: %d %s", resp.StatusCode, string(body))
	}

	// 记录开始时间（用于日志）
	startTime := time.Now()
	var requestTokens, responseTokens int
	status := "success"
	var errorMsg string

	defer func() {
		// 记录日志
		log := gaia.ModelProxyLog{
			UserId:         userID,
			ProviderName:   providerName,
			ModelName:      req.Model,
			RequestTokens:  requestTokens,
			ResponseTokens: responseTokens,
			Status:         status,
			ErrorMessage:   errorMsg,
			CreatedAt:      startTime,
		}
		global.GVA_DB.Create(&log)
	}()

	// 处理流式响应
	if req.Stream {
		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Text()
			if _, err := writer.Write([]byte(line + "\n")); err != nil {
				status = "error"
				errorMsg = err.Error()
				return err
			}
			// Flush if writer supports it
			if flusher, ok := writer.(http.Flusher); ok {
				flusher.Flush()
			}
		}
		if err := scanner.Err(); err != nil {
			status = "error"
			errorMsg = err.Error()
			return err
		}
	} else {
		// 非流式响应
		if _, err := io.Copy(writer, resp.Body); err != nil {
			status = "error"
			errorMsg = err.Error()
			return err
		}
	}

	return nil
}

// getProviderByModel 根据模型名称推断所属提供商短名（openai/tongyi/google/anthropic）。
func (s *ModelProviderService) getProviderByModel(modelName string) (string, error) {
	modelLower := strings.ToLower(modelName)
	if strings.HasPrefix(modelLower, "gpt") || strings.Contains(modelLower, "openai") {
		return gaia.ProviderOpenai, nil
	}
	if strings.HasPrefix(modelLower, "qwen") || strings.Contains(modelLower, "tongyi") {
		return gaia.ProviderTongyi, nil
	}
	if strings.HasPrefix(modelLower, "gemini") || strings.Contains(modelLower, "google") {
		return gaia.ProviderGoogle, nil
	}
	if strings.Contains(modelLower, "claude") || strings.Contains(modelLower, "anthropic") {
		return gaia.ProviderAnthropic, nil
	}
	return "", fmt.Errorf("无法识别模型 %s 的提供商", modelName)
}

// isModelEnabled 检查指定提供商下该模型是否在已启用且已选模型列表中。
func (s *ModelProviderService) isModelEnabled(providerName, modelName string) bool {
	var config gaia.ModelProviderConfig
	if err := global.GVA_DB.Where("provider_name = ? AND enabled = ?", providerName, true).First(&config).Error; err != nil {
		return false
	}

	var models []string
	if err := json.Unmarshal([]byte(config.Models), &models); err != nil {
		return false
	}

	for _, m := range models {
		if m == modelName {
			return true
		}
	}

	return false
}

// getUpstreamEndpoint 根据提供商短名返回聊天补全接口的上游 URL。
func (s *ModelProviderService) getUpstreamEndpoint(providerName string) string {
	if endpoint, ok := gaia.DefaultChatCompletionsEndpoints[providerName]; ok {
		return endpoint
	}
	return ""
}

// getUpstreamBase 返回提供商的上游根地址（用于通用代理）。优先使用 provider_credentials 的 openai_api_base（如 "https://yunwu.ai"），便于计费与多租户区分。
func (s *ModelProviderService) getUpstreamBase(providerName string, creds *gaiaResponse.ProviderCredentials) string {
	if creds != nil && strings.TrimSpace(creds.Endpoint) != "" {
		return strings.TrimSuffix(strings.TrimSpace(creds.Endpoint), "/")
	}
	if base, ok := gaia.DefaultAPIBase[providerName]; ok {
		return strings.TrimSuffix(base, "/")
	}
	return ""
}

// ProxyRequest 将任意路径的请求转发到上游（anthropic /v1/messages、gemini /v1beta/...、openai /v1/chat/completions、/v1/images/generations、/v1/embeddings 等）。
// @Tags System Integrated
// @Summary 通用代理请求
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// provider 可通过 X-Gaia-Provider 头、query provider= 或 body 中的 model 字段推断；上游 base 优先使用 creds.Endpoint（openai_api_base）。
func (s *ModelProviderService) ProxyRequest(
	userID, path, method string, reqHeader http.Header, body []byte, writer io.Writer) (err error) {
	// init
	var providerName string
	if path = strings.TrimPrefix(path, "/"); path == "" {
		return fmt.Errorf("代理路径不能为空")
	}

	// 解析 provider：头 > query 已在 handler 传入；此处从 body 取 model 仅当 body 为 JSON 且含 model 时用于推断
	if p := reqHeader.Get("X-Gaia-Provider"); p != "" {
		providerName = strings.TrimSpace(strings.ToLower(p))
	}
	if providerName == "" && len(body) > 0 {
		var obj map[string]interface{}
		if err = json.Unmarshal(body, &obj); err == nil {
			if m, ok := obj["model"].(string); ok && m != "" {
				var errP error
				providerName, errP = s.getProviderByModel(m)
				if errP != nil {
					return errP
				}
				// 有 model 时校验该模型是否在开启列表
				if !s.isModelEnabled(providerName, m) {
					return fmt.Errorf("模型 %s 未开启", m)
				}
			}
		}
	}
	if providerName == "" {
		return fmt.Errorf("请指定 provider：设置请求头 X-Gaia-Provider 或 query provider=，或在 body 中提供 model 字段")
	}

	// 若未从 body model 解析出 provider，则只校验该提供商已启用
	if !s.isProviderEnabled(providerName) {
		return fmt.Errorf("提供商 %s 未开启", providerName)
	}

	var base string
	var bodyReader io.Reader
	var creds *gaiaResponse.ProviderCredentials
	if creds, err = s.GetDifyProviderCredentials(providerName); err != nil {
		return err
	}

	if base = s.getUpstreamBase(providerName, creds); base == "" {
		return fmt.Errorf("提供商 %s 无可用上游地址", providerName)
	}

	if len(body) > 0 {
		bodyReader = bytes.NewReader(body)
	}
	fmt.Println("path", base+"/"+path, string(body))
	httpReq, err := http.NewRequest(method, base+"/"+path, bodyReader)
	if err != nil {
		return err
	}

	// 复制常用请求头，Authorization 使用上游 API Key
	httpReq.Header.Set("Authorization", "Bearer "+creds.APIKey)
	if ct := reqHeader.Get("Content-Type"); ct != "" {
		httpReq.Header.Set("Content-Type", ct)
	}
	if accept := reqHeader.Get("Accept"); accept != "" {
		httpReq.Header.Set("Accept", accept)
	}
	// 流式请求
	if reqHeader.Get("Accept") == "text/event-stream" || reqHeader.Get("Accept") == "" {
		// 不强制覆盖，上游可能根据 body 的 stream 返回 SSE
	}

	client := &http.Client{Timeout: 5 * time.Minute}
	resp, err := client.Do(httpReq)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	// 记录代理日志（用于计费时可区分 openai_api_base）
	startTime := time.Now()
	modelOrPath := path
	if len(body) > 0 {
		var obj map[string]interface{}
		if json.Unmarshal(body, &obj) == nil {
			if m, _ := obj["model"].(string); m != "" {
				modelOrPath = m
			}
		}
	}
	var logStatus, logError string
	defer func() {
		if logStatus == "" {
			logStatus = "success"
		}
		global.GVA_DB.Create(&gaia.ModelProxyLog{
			UserId:       userID,
			ProviderName: providerName,
			ModelName:    modelOrPath,
			Status:       logStatus,
			ErrorMessage: logError,
			CreatedAt:    startTime,
		})
	}()

	// 写回状态码与响应头（流式由上游 Content-Type 决定）
	if w, ok := writer.(http.ResponseWriter); ok {
		for k, v := range resp.Header {
			for _, vv := range v {
				w.Header().Add(k, vv)
			}
		}
		w.WriteHeader(resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		_, _ = io.Copy(writer, resp.Body)
		return nil
	}
	// 流式响应时按行刷新，避免缓冲
	if strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		if flusher, ok := writer.(http.Flusher); ok {
			scanner := bufio.NewScanner(resp.Body)
			for scanner.Scan() {
				fmt.Println("sss", scanner.Text())
				if _, err = writer.Write([]byte(scanner.Text() + "\n")); err != nil {
					logStatus, logError = "error", err.Error()
					return err
				}
				flusher.Flush()
			}
			if err = scanner.Err(); err != nil {
				logStatus, logError = "error", err.Error()
				return err
			}
			return nil
		}
	}
	_, err = io.Copy(writer, resp.Body)
	if err != nil {
		logStatus, logError = "error", err.Error()
	}
	return err
}

// isProviderEnabled 检查该提供商是否已启用（未校验具体模型列表，用于通用代理）。
func (s *ModelProviderService) isProviderEnabled(providerName string) bool {
	var config gaia.ModelProviderConfig
	if err := global.GVA_DB.Where("provider_name = ? AND enabled = ?", providerName, true).First(&config).Error; err != nil {
		return false
	}
	return true
}
