package gaia

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	gaiaReq "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/service"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

type ModelProviderApi struct{}

var modelProviderService = service.ServiceGroupApp.GaiaServiceGroup.ModelProviderService

// GetProviderList 获取提供商配置列表
// @Tags ModelProvider
// @Summary 获取提供商配置列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response{data=[]gaiaResponse.ProviderListItem,msg=string} "获取成功"
// @Router /gaia/model-provider/list [get]
func (m *ModelProviderApi) GetProviderList(c *gin.Context) {
	list, err := modelProviderService.GetProviderList()
	if err != nil {
		global.GVA_LOG.Error("获取提供商配置列表失败", zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	response.OkWithData(list, c)
}

// UpdateProviderConfig 更新提供商配置
// @Tags ModelProvider
// @Summary 更新提供商配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body object true "提供商配置"
// @Success 200 {object} response.Response{msg=string} "更新成功"
// @Router /gaia/model-provider/update [post]
func (m *ModelProviderApi) UpdateProviderConfig(c *gin.Context) {
	var req struct {
		ProviderName string   `json:"provider_name" binding:"required"`
		Enabled      bool     `json:"enabled"`
		Models       []string `json:"models"`
	}

	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage("参数错误:"+err.Error(), c)
		return
	}

	if err := modelProviderService.UpdateProviderConfig(req.ProviderName, req.Enabled, req.Models); err != nil {
		global.GVA_LOG.Error("更新提供商配置失败", zap.String("provider", req.ProviderName), zap.Error(err))
		response.FailWithMessage("更新失败:"+err.Error(), c)
		return
	}

	response.OkWithMessage("更新成功", c)
}

// GetModels 获取开启的模型列表（OpenAI 格式，供第三方兼容调用；成功时返回裸 JSON，错误时与项目统一使用 response）。
// @Tags ModelProvider
// @Summary 获取开启的模型列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} gaiaResponse.OpenAIModelsResponse "获取成功"
// @Router /gaia/models [get]
func (m *ModelProviderApi) GetModels(c *gin.Context) {
	models, err := modelProviderService.GetEnabledModels()
	if err != nil {
		global.GVA_LOG.Error("获取模型列表失败", zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	c.JSON(http.StatusOK, models)
}

// proxyWithAccountId 通用代理逻辑：按路径转发到上游并计费。
func proxyWithAccountId(c *gin.Context, accountId string) {
	path := c.Param("path")
	if path == "" || path == "/" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "代理路径不能为空"}})
		return
	}
	reqHeader := c.Request.Header.Clone()
	if q := strings.TrimSpace(c.Query("provider")); q != "" {
		reqHeader.Set("X-Gaia-Provider", q)
	}
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "读取请求体失败"}})
		return
	}
	var bodyModel string
	if len(body) > 0 {
		var parseObj map[string]interface{}
		if jsonErr := json.Unmarshal(body, &parseObj); jsonErr == nil {
			if mv, ok := parseObj["model"].(string); ok {
				bodyModel = mv
			}
		}
	}
	global.GVA_LOG.Info("Gaia代理请求入参",
		zap.String("account_id", accountId),
		zap.String("path", path),
		zap.String("method", c.Request.Method),
		zap.Int("body_len", len(body)),
		zap.String("body_model", bodyModel),
	)

	// 余额前置检查：余额耗尽时直接拦截，不继续请求上游
	if quotaErr := modelProviderService.CheckAccountQuota(accountId); quotaErr != nil {
		c.JSON(http.StatusPaymentRequired, gin.H{"error": gin.H{"message": quotaErr.Error()}})
		return
	}

	if err = modelProviderService.ProxyRequest(
		accountId, path, c.Request.Method, reqHeader, body, c.Writer); err != nil {
		global.GVA_LOG.Error("代理请求失败", zap.String("account_id", accountId), zap.String("path", path), zap.Error(err))
		if !c.Writer.Written() {
			c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"message": err.Error()}})
		}
	}
}

// Proxy 通用中转 API：将 /gaia/proxy/* 的请求按路径转发到上游（需 JWT，account 来自当前登录用户）。
// @Tags ModelProvider
// @Summary 通用中转API（按路径转发）
// @Security ApiKeyAuth
// @Param path path string true "上游路径，如 v1/chat/completions、v1/messages"
// @Router /gaia/proxy/*path [get,post,put,patch,delete]
func (m *ModelProviderApi) Proxy(c *gin.Context) {
	accountId := utils.GetUserUuid(c).String()
	proxyWithAccountId(c, accountId)
}

// GetAvailableModels 获取提供商的可用模型
// @Tags ModelProvider
// @Summary 获取提供商的可用模型
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param provider_name query string true "提供商名称"
// @Success 200 {object} response.Response{data=[]gaiaResponse.ModelInfo,msg=string} "获取成功"
// @Router /gaia/model-provider/available-models [get]
func (m *ModelProviderApi) GetAvailableModels(c *gin.Context) {
	providerName := c.Query("provider_name")
	if providerName == "" {
		response.FailWithMessage("参数错误:provider_name不能为空", c)
		return
	}
	models, err := modelProviderService.GetAvailableModelsFromDify(providerName)
	if err != nil {
		global.GVA_LOG.Error("获取可用模型失败", zap.String("provider", providerName), zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	response.OkWithData(models, c)
}

// TestProviderCredentials 测试提供商凭证
// @Tags ModelProvider
// @Summary 测试提供商凭证
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param provider_name query string true "提供商名称"
// @Success 200 {object} response.Response{msg=string} "测试成功"
// @Router /gaia/model-provider/test-credentials [get]
func (m *ModelProviderApi) TestProviderCredentials(c *gin.Context) {
	providerName := c.Query("provider_name")
	if providerName == "" {
		response.FailWithMessage("参数错误:provider_name不能为空", c)
		return
	}
	creds, err := modelProviderService.GetDifyProviderCredentials(providerName)
	if err != nil {
		global.GVA_LOG.Error("获取提供商凭证失败", zap.String("provider", providerName), zap.Error(err))
		response.FailWithMessage("获取凭证失败:"+err.Error(), c)
		return
	}

	var result map[string]interface{}

	// AWS Bedrock：展示 access key 信息
	if creds.AWSAccessKeyID != "" {
		maskedKey := "****"
		if len(creds.AWSAccessKeyID) > 8 {
			maskedKey = creds.AWSAccessKeyID[:4] + "****" + creds.AWSAccessKeyID[len(creds.AWSAccessKeyID)-4:]
		}
		region := creds.AWSRegion
		if region == "" {
			region = "us-east-1（默认）"
		}
		result = map[string]interface{}{
			"provider":           providerName,
			"has_api_key":        true,
			"api_key":            maskedKey,
			"aws_access_key_id":  maskedKey,
			"aws_region":         region,
			"has_session_token":  creds.AWSSessionToken != "",
		}
	} else {
		// 隐藏API Key的大部分内容
		maskedKey := ""
		if len(creds.APIKey) > 8 {
			maskedKey = creds.APIKey[:4] + "****" + creds.APIKey[len(creds.APIKey)-4:]
		} else {
			maskedKey = "****"
		}
		result = map[string]interface{}{
			"provider":    providerName,
			"has_api_key": creds.APIKey != "",
			"api_key":     maskedKey,
		}
	}

	response.OkWithData(result, c)
}

// GetProxyLogs 获取代理日志（分页）
// @Tags ModelProvider
// @Summary 获取代理日志
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param page query int false "页码"
// @Param page_size query int false "每页数量"
// @Success 200 {object} response.Response{data=response.PageResult,msg=string} "获取成功"
// @Router /gaia/model-provider/logs [get]
func (m *ModelProviderApi) GetProxyLogs(c *gin.Context) {
	var req gaiaReq.GetProxyLogsReq
	if err := c.ShouldBindQuery(&req); err != nil {
		response.FailWithMessage("参数错误:"+err.Error(), c)
		return
	}
	if req.Page < 1 {
		req.Page = 1
	}
	if req.PageSize < 1 || req.PageSize > 100 {
		req.PageSize = 20
	}
	list, total, err := modelProviderService.GetProxyLogs(req)
	if err != nil {
		global.GVA_LOG.Error("获取代理日志失败", zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	response.OkWithDetailed(response.PageResult{
		List:     list,
		Total:    total,
		Page:     req.Page,
		PageSize: req.PageSize,
	}, "获取成功", c)
}
